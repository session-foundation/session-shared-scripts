#!/usr/bin/env python3
"""
Reconcile the open duplicate-translation slots with Crowdin, and post what changed.

Every string of every scanned locale is judged against the state: slots newly holding
2+ translations are posted as new, slots no longer holding them as resolved, and
nothing is posted when nothing changed. A slot whose string or locale left the
project resolves.

With --croql, what the timer runs, a locale costs one query for the strings holding
2+ translations in it at all, then one request per candidate. Without it, one request
per string. A plural string with one translation per category is a candidate too;
CroQL cannot tell plural categories apart.

One run at a time: a second one started while another holds the state's lock exits.

The state is written only after every message landed, so a failed post is repeated in
full on the next run rather than lost.

Config (env vars):
    CROWDIN_API_TOKEN           read-only Crowdin token
    CROWDIN_DISCORD_WEBHOOK_URL where changes go (not needed with --dry-run or --seed)

Usage:
    crowdin-reconcile-duplicates --seed --state PATH   # first run: record, post nothing
    crowdin-reconcile-duplicates --reseed --state PATH # the same over an existing state
    crowdin-reconcile-duplicates --state PATH          # what the timer does
    crowdin-reconcile-duplicates --dry-run --state PATH --locales de
"""
import argparse
import concurrent.futures
import json
import os
import sys

from session_ops.crowdin import duplicates, sdk
from session_ops.shared import discord, http
from session_ops.shared.env import get_env

DEFAULT_PROJECT = "618696"
CROQL = 'count of translations where ( language = @language:"{lang}" ) > 1'


def crowdin_client(token, project_id):
    return sdk.client(token, project_id, attempts=10, timeout=60)


def candidates(client, lang, string_ids, use_croql):
    if not use_croql:
        return list(string_ids)
    rows = sdk.fetch_all(client.source_strings, "list_strings", croql=CROQL.format(lang=lang))
    return [row["id"] for row in rows]


def scan_locale(client, project, lang, strings, use_croql, max_workers):
    """(findings, judged) for one locale. A string that failed is left unjudged."""
    approved = {}
    for a in sdk.fetch_all(client.string_translations, "list_translation_approvals",
                           languageId=lang):
        approved.setdefault(a["stringId"], set()).add(a["translationId"])
    todo = candidates(client, lang, strings, use_croql)
    # Everything CroQL left out holds at most one translation here, as of its query.
    judged = {duplicates.scope_key(sid, lang) for sid in strings}

    def check(sid):
        return duplicates.check_string(client, sid, lang, strings.get(sid, {}),
                                       project.editor_url(lang, sid), approved.get(sid, set()))

    findings, failed = [], 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(check, sid): sid for sid in todo}
        for future in concurrent.futures.as_completed(futures):
            sid = futures[future]
            try:
                found = future.result()
            except Exception as exc:
                failed += 1
                judged.discard(duplicates.scope_key(sid, lang))
                print(f"[{lang}] string {sid} failed, left unjudged: {exc!r}", file=sys.stderr)
                continue
            findings.extend(found)
    print(f"[{lang}] {len(todo)} checked, {failed} failed, {len(findings)} slot(s) open",
          file=sys.stderr)
    return findings, judged


def vanished(state, strings, locales):
    """Scopes whose string or locale left the project: judged with no findings, so their
    slots resolve. `locales` is None when only some locales were scanned."""
    return {duplicates.scope_key(slot["stringId"], slot["locale"])
            for slot in state["slots"].values()
            if slot["stringId"] not in strings
            or (locales is not None and slot["locale"] not in locales)}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Reconcile Crowdin duplicate translations.")
    parser.add_argument("--project-id", default=DEFAULT_PROJECT)
    parser.add_argument("--state", required=True, metavar="PATH",
                        help="The open slots.")
    parser.add_argument("--locales", nargs="+", help="Only these (default: every target).")
    parser.add_argument("--croql", action="store_true",
                        help="Narrow each locale with a CroQL query before checking strings.")
    parser.add_argument("--max-workers", type=int, default=16)
    parser.add_argument("--seed", action="store_true",
                        help="Record what is open without posting it; refuses an existing state.")
    parser.add_argument("--reseed", action="store_true",
                        help="--seed over an existing state, absorbing unposted what changed since.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would be posted; write nothing.")
    args = parser.parse_args(argv)
    if args.seed and os.path.exists(args.state):
        sys.exit(f"{args.state} already exists, and --seed would absorb every change since "
                 f"it was written without posting it. Use --reseed to do that on purpose.")
    args.seed = args.seed or args.reseed
    if not (args.seed or args.dry_run):
        duplicates.load(args.state)  # refuses a missing state before the scan, not after
    with duplicates.only_run(args.state):
        reconcile(args)


def reconcile(args):
    token = get_env("CROWDIN_API_TOKEN")
    webhook = get_env("CROWDIN_DISCORD_WEBHOOK_URL",
                      required=not (args.dry_run or args.seed))
    client = crowdin_client(token, args.project_id)
    project = duplicates.Project(client.projects.get_project()["data"])
    locales = args.locales or project.locales
    strings = {s["id"]: {"identifier": s.get("identifier"), "text": s.get("text")}
               for s in sdk.fetch_all(client.source_strings, "list_strings")}
    print(f"{len(strings)} strings, {len(locales)} locale(s)", file=sys.stderr)

    findings, judged = [], set()
    for lang in locales:
        try:
            found, scanned = scan_locale(client, project, lang, strings, args.croql,
                                         args.max_workers)
        except sdk.APIException as exc:
            print(f"[{lang}] skipped, left unjudged: {exc.http_status} "
                  f"{sdk.error_message(exc)}", file=sys.stderr)
            continue
        findings += found
        judged |= scanned
    if not judged:
        sys.exit("No locale could be scanned.")

    state = duplicates.load(args.state, missing_ok=args.seed or args.dry_run)
    judged |= vanished(state, strings, None if args.locales else project.locales)
    opened, resolved = duplicates.apply(state, findings, judged, duplicates.now())
    print(f"{len(opened)} opened, {len(resolved)} resolved, "
          f"{len(state['slots'])} open", file=sys.stderr)
    if args.dry_run:
        messages = duplicates.build_messages(opened, resolved, len(state["slots"]), project)
        print(json.dumps(messages, indent=2, ensure_ascii=False))
        return
    if not args.seed:
        messages = duplicates.build_messages(opened, resolved, len(state["slots"]), project)
        posted = discord.post_to_discord(http.Session(), webhook, messages) if messages else 0
        if posted < len(messages):
            sys.exit(f"Posted {posted} of {len(messages)} messages; state not written, "
                     f"so the next run repeats them.")
    duplicates.save(args.state, state)


if __name__ == "__main__":
    main()
