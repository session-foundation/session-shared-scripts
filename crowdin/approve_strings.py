#!/usr/bin/env python3
"""
Bulk-approve specific Crowdin source strings across all target languages.

Crowdin.com (API v2). Approval requires a Proofreader/Manager token, scoped with
`project` + `project.source` (listing strings) + `project.translation` (reading
translations, adding approvals) -- a missing scope shows up as a 403 on just the
endpoints it covers.

The Crowdin API token is read from the system keyring via libsecret's
`secret-tool`. Store it once with:

    secret-tool store --label='Crowdin Translation API token' service crowdin key translation-api-token

Usage:
    # inspect first: list every translation + who submitted it (no changes)
    python approve_strings.py --list ongoingAppeal ongoingAppealDescription

    # approve ONLY translations submitted by trusted users (by username or id):
    python approve_strings.py --by-user alice --by-user 12345 ongoingAppeal ongoingAppealDescription

    # dry-run of the above (shows what would be approved, changes nothing):
    python approve_strings.py --dry-run --by-user alice ongoingAppeal ongoingAppealDescription

Exactly one of --by-user or --all-users is required unless --list is given, so
approval is always an explicit choice -- a forgotten filter is an error, never
a silent "approve anything".

(The CROWDIN_API_TOKEN environment variable, if set, is used as a fallback.)
"""
import argparse
import os
import subprocess
import sys
import time

import requests

API = "https://api.crowdin.com/api/v2"
DEFAULT_PROJECT = "618696"

# libsecret attributes identifying the token in the keyring; must match the
# `secret-tool store ...` attributes used to save it.
KEYRING_ATTRS = ["service", "crowdin", "key", "translation-api-token"]


def get_token():
    """Read the Crowdin API token from the system keyring via libsecret, falling back to env."""
    try:
        out = subprocess.run(
            ["secret-tool", "lookup", *KEYRING_ATTRS],
            capture_output=True, text=True, check=True,
        )
        token = out.stdout.strip()
        if token:
            return token
    except FileNotFoundError:
        pass  # secret-tool not installed
    except subprocess.CalledProcessError:
        pass  # no matching secret in the keyring

    token = os.environ.get("CROWDIN_API_TOKEN")
    if token:
        return token

    sys.exit(
        "No Crowdin API token found.\n"
        "Store it in the system keyring with:\n"
        "  secret-tool store --label='Crowdin Translation API token' "
        + " ".join(KEYRING_ATTRS)
        + "\n(or set CROWDIN_API_TOKEN in the environment as a fallback)."
    )


def describe_user(u):
    """Human-readable submitter for a translation's user object (may be None)."""
    if not u:
        return "submitter=<none/MT>"
    name = u.get("username") or u.get("fullName") or "?"
    return f"submitter={u.get('id')}:{name}"


def user_allowed(u, allowed):
    """True if translation user u matches the allowed set (by id, username, or fullName).

    allowed is a set of lowercased strings; empty set means 'no filter' (allow all)."""
    if not allowed:
        return True
    if not u:
        return False  # unknown submitter is never auto-allowed under a filter
    candidates = {
        str(u.get("id")).lower(),
        (u.get("username") or "").lower(),
        (u.get("fullName") or "").lower(),
    }
    return bool(candidates & allowed)


def snippet(text, n=60):
    text = (text or "").replace("\n", " ")
    return text[:n] + ("…" if len(text) > n else "")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("identifiers", nargs="+",
                   help="one or more string identifiers (the name= keys) to approve, matched exactly")
    p.add_argument("--project-id", default=DEFAULT_PROJECT)
    p.add_argument("--dry-run", action="store_true",
                   help="show what would be approved without approving")
    p.add_argument("--list", action="store_true",
                   help="list every translation and its submitter for the strings; approve nothing")
    who = p.add_mutually_exclusive_group()
    who.add_argument("--by-user", action="append", dest="by_user", metavar="USER",
                     help="only approve translations submitted by this user (username, full name, "
                          "or numeric id); repeatable.")
    who.add_argument("--all-users", action="store_true",
                     help="approve regardless of submitter -- explicit opt-in required to skip "
                          "the --by-user filter.")
    args = p.parse_args()

    # Approval must be explicitly scoped: forgetting the filter must NOT mean
    # "approve anything". Require exactly one of --by-user / --all-users unless listing.
    if not args.list and not args.by_user and not args.all_users:
        p.error("specify --by-user USER (repeatable) or --all-users (explicit opt-in), "
                "or use --list to inspect without approving")

    token = get_token()

    identifiers = args.identifiers
    pid = args.project_id
    s = requests.Session()
    s.headers.update({"Authorization": f"Bearer {token}"})

    def get(path, **params):
        r = s.get(f"{API}{path}", params=params, timeout=30)
        r.raise_for_status()
        return r.json()

    # 1) Resolve identifiers -> string IDs
    string_ids = {}
    for ident in identifiers:
        found = None
        offset = 0
        while True:
            data = get(f"/projects/{pid}/strings", filter=ident,
                       scope="identifier", limit=500, offset=offset)["data"]
            for row in data:
                if row["data"]["identifier"] == ident:
                    found = row["data"]["id"]
                    break
            if found or len(data) < 500:
                break
            offset += 500
        if not found:
            sys.exit(f"Could not find a string with identifier '{ident}'.")
        string_ids[ident] = found
        print(f"  string '{ident}' -> id {found}")

    # 2) Target languages
    proj = get(f"/projects/{pid}")["data"]
    target_langs = proj["targetLanguageIds"]
    print(f"\n{len(target_langs)} target languages: {', '.join(target_langs)}\n")

    # empty allowed set == allow any submitter (only reachable via --all-users or --list)
    allowed = {a.lower() for a in (args.by_user or [])}

    # 3) For each string/language: list, or approve the newest translation from an allowed submitter
    approved, skipped, already, errors = 0, 0, 0, 0
    for ident, sid in string_ids.items():
        for lang in target_langs:
            raw = get(f"/projects/{pid}/translations",
                      stringId=sid, languageId=lang, limit=500)["data"]
            trans = [t["data"] for t in raw]

            if args.list:
                # Show only unapproved translations (i.e. what a run would approve),
                # honouring the --by-user filter if one was given.
                approvals = get(f"/projects/{pid}/approvals",
                                stringId=sid, languageId=lang, limit=500)["data"]
                approved_tids = {a["data"]["translationId"] for a in approvals}
                pending = [t for t in trans
                           if t["id"] not in approved_tids
                           and user_allowed(t.get("user"), allowed)]
                for t in pending:
                    print(f"  {ident} / {lang}: tid={t['id']} {describe_user(t.get('user'))} "
                          f"| {snippet(t.get('text'))}")
                continue

            if not trans:
                print(f"  [skip] {ident} / {lang}: no translation")
                skipped += 1
                continue

            eligible = [t for t in trans if user_allowed(t.get("user"), allowed)]
            if not eligible:
                present = ", ".join(sorted({describe_user(t.get("user")) for t in trans}))
                print(f"  [skip] {ident} / {lang}: no translation from an allowed submitter "
                      f"(present: {present})")
                skipped += 1
                continue

            chosen = max(eligible, key=lambda t: t["id"])
            tid = chosen["id"]
            info = f"tid={tid} {describe_user(chosen.get('user'))} | {snippet(chosen.get('text'))}"

            if args.dry_run:
                print(f"  [dry ] {ident} / {lang}: would approve {info}")
                approved += 1
                continue

            r = s.post(f"{API}/projects/{pid}/approvals",
                       json={"translationId": tid}, timeout=30)
            if r.status_code == 201:
                print(f"  [ ok ] {ident} / {lang}: approved {info}")
                approved += 1
            elif r.status_code == 400 and "already" in r.text.lower():
                print(f"  [ -- ] {ident} / {lang}: already approved {info}")
                already += 1
            else:
                print(f"  [ERR ] {ident} / {lang}: {r.status_code} {r.text}")
                errors += 1
            time.sleep(0.1)  # gentle on rate limits

    if args.list:
        return
    verb = "would approve" if args.dry_run else "approved"
    print(f"\nDone. {verb}: {approved}, already: {already}, skipped: {skipped}, errors: {errors}")


if __name__ == "__main__":
    main()
