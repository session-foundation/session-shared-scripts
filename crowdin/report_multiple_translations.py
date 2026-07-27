#!/usr/bin/env python3
"""
Report Crowdin string slots that have MORE THAN ONE translation, so a human can
pick which single translation to keep and delete the rest.

The goal is exactly one translation per string -- and, for strings with plural
forms, exactly one translation per plural category (one, few, many, other, …).
A "slot" here is a (string, plural-category) pair. Any slot that currently holds
2+ translations needs our attention: whichever one is exported is ambiguous, and
someone has to choose the winner.

Detecting duplicates per plural-category needs one API request per
(string, locale) -- no Crowdin endpoint ties a translation to its string in
bulk -- and Crowdin rate-limits at ~40 req/s, so scanning all ~80 locales at
once takes ~40 min. To keep a daily job under ~10 min this ROTATES: each run
scans one shard of the locales (default 1/8, ~10 locales), and today's shard is
chosen from the date, so the full set is covered over an 8-day cycle. Use
--all-locales for a single complete (slow) scan, or --locales to pick some.

Findings are posted to a Discord webhook as a summary embed plus per-locale
breakdowns linking straight to the Crowdin editor for each slot. Use --json to
also dump the raw findings (the same shape find_pending_user_suggestions.py
--list-multiple produces, so it can be fed to delete_from_findings.py /
delete_stale_unvoted.py).

The Crowdin API token is read (in order) from:
  * --api-token / first positional arg,
  * the CROWDIN_API_TOKEN environment variable,
  * the system keyring via `secret-tool` (service=crowdin key=translation-api-token).

The Discord webhook is read from --webhook, else DISCORD_WEBHOOK_URL. With
--dry-run the payloads are printed instead of posted (no webhook required).

Usage:
    python report_multiple_translations.py --dry-run        # scan + print, post nothing
    python report_multiple_translations.py                  # scan + post to Discord
    python report_multiple_translations.py --json multi.json # also write findings JSON
"""
import argparse
import collections
import concurrent.futures
import datetime as dt
import email.utils
import json
import os
import subprocess
import sys
import threading
import time

import requests

API = "https://api.crowdin.com/api/v2"
DEFAULT_PROJECT = "618696"
KEYRING_ATTRS = ["service", "crowdin", "key", "translation-api-token"]

# Discord limits: 10 embeds/message, 4096 chars/description, 25 fields, 6000
# chars total/message. We keep each description under MAX_DESC_CHARS and pack
# messages under both MAX_EMBEDS_PER_MESSAGE and MAX_MESSAGE_CHARS.
MAX_EMBEDS_PER_MESSAGE = 10
MAX_DESC_CHARS = 3800
MAX_MESSAGE_CHARS = 6000
# Hard cap on how many slots we enumerate in Discord (the rest are summarised as
# an overflow count -- run with --json to get the complete list).
MAX_SLOTS_LISTED = 200


def get_token(cli_token):
    if cli_token:
        return cli_token
    if os.environ.get("CROWDIN_API_TOKEN"):
        return os.environ["CROWDIN_API_TOKEN"]
    try:
        out = subprocess.run(["secret-tool", "lookup", *KEYRING_ATTRS],
                             capture_output=True, text=True, check=True)
        if out.stdout.strip():
            return out.stdout.strip()
    except (FileNotFoundError, subprocess.CalledProcessError):
        pass
    sys.exit("No Crowdin API token found (pass --api-token, set CROWDIN_API_TOKEN, "
             "or store it via secret-tool).")


def parse_retry_after(value, fallback, cap=30):
    """Seconds to wait for a Retry-After header, parsed defensively.

    Supports both numeric-seconds and HTTP-date forms (RFC 7231); falls back to
    `fallback` when the header is missing or unparseable, and clamps the result
    to `cap` so a bogus/huge value can't stall the run."""
    wait = fallback
    if value is not None:
        try:
            wait = float(value)
        except (TypeError, ValueError):
            try:
                when = email.utils.parsedate_to_datetime(value)
                if when.tzinfo is None:
                    when = when.replace(tzinfo=dt.timezone.utc)
                wait = (when - dt.datetime.now(dt.timezone.utc)).total_seconds()
            except (TypeError, ValueError):
                wait = fallback
    if wait < 0:
        wait = fallback
    return min(wait, cap)


def request_with_retry(session, method, url, max_retries=10, **kw):
    """Request with backoff on 429/5xx AND on network errors (flaky DNS/connection)."""
    delay = 0.5
    last_exc = None
    for _ in range(max_retries):
        try:
            r = session.request(method, url, timeout=60, **kw)
        except requests.exceptions.RequestException as e:
            last_exc = e
            time.sleep(delay)
            delay = min(delay * 2, 30)
            continue
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(parse_retry_after(r.headers.get("Retry-After"), delay))
            delay = min(delay * 2, 30)
            continue
        r.raise_for_status()
        return r
    if last_exc:
        raise last_exc
    r.raise_for_status()
    return r


def paged(session, path, **params):
    """Yield every item from a paginated Crowdin list endpoint."""
    offset = 0
    limit = 500
    while True:
        r = request_with_retry(session, "GET", f"{API}{path}",
                               params={**params, "limit": limit, "offset": offset})
        data = r.json()["data"]
        for row in data:
            yield row["data"]
        if len(data) < limit:
            return
        offset += limit


def user_label(u):
    if not u:
        return "<none/MT>"
    return f"{u.get('id')}:{u.get('username') or u.get('fullName') or '?'}"


def snippet(text, n=70):
    text = (text or "").replace("\n", " ")
    return text[:n] + ("…" if len(text) > n else "")


def clip(text, n):
    text = text or ""
    return text if len(text) <= n else text[: n - 1] + "…"


# --------------------------------------------------------------------------- #
# Scanning
# --------------------------------------------------------------------------- #
# Speed note: detecting duplicates PER plural-category requires grouping
# translations by (string, category), and the only Crowdin endpoint that ties a
# translation to its string is the per-string translations list. (The language-
# wide sweep -- ?languageId=xx with no stringId -- is cheap but omits stringId,
# and neither a single-translation GET nor CroQL exposes it either.) So we must
# do one request per (string, language). Crowdin rate-limits at ~40 req/s, so a
# full 80-locale scan is ~1366*80 requests ≈ 40 min. To keep a daily job fast we
# scan a ROTATING shard of locales each run (see pick_locales), covering them all
# over a cycle. Within a locale we fan strings out across a bounded thread pool.
def scan_locale(session, pid, lang, string_ids, strings, editor_url, max_workers):
    """Return the finding dicts for one locale (one slot per 2+-translation group)."""
    approved_here = {}  # stringId -> set(translationId) that are approved
    for a in paged(session, f"/projects/{pid}/approvals", languageId=lang):
        approved_here.setdefault(a["stringId"], set()).add(a["translationId"])
    print(f"[{lang}] scanning {len(string_ids)} strings "
          f"({len(approved_here)} with approvals) …", file=sys.stderr)

    # requests.Session is not guaranteed thread-safe, so instead of sharing the
    # caller's session across the pool, give each worker its own (lazily created,
    # carrying the same auth header).
    tls = threading.local()

    def worker_session():
        s = getattr(tls, "session", None)
        if s is None:
            s = requests.Session()
            s.headers.update(session.headers)
            tls.session = s
        return s

    def process(sid):
        trans = list(paged(worker_session(), f"/projects/{pid}/translations",
                           stringId=sid, languageId=lang))
        approved_tids = approved_here.get(sid, set())
        by_cat = collections.defaultdict(list)
        for t in trans:
            by_cat[t.get("pluralCategoryName")].append(t)
        local = []
        for cat, ts in by_cat.items():
            if len(ts) < 2:
                continue  # sole translation in its category -> used as-is, nothing to review
            ts.sort(key=lambda t: t.get("createdAt") or "")
            local.append({
                "locale": lang,
                "status": "multiple-translations",
                "stringId": sid,
                "identifier": strings.get(sid, {}).get("identifier"),
                "webUrl": editor_url(lang, sid),
                "pluralCategory": cat,
                "count": len(ts),
                "sourceText": strings.get(sid, {}).get("text"),
                "translations": [{
                    "translationId": t["id"],
                    "user": user_label(t.get("user")),
                    "createdAt": t.get("createdAt"),
                    "approved": t["id"] in approved_tids,
                    "rating": t.get("rating"),
                    "isPreTranslated": t.get("isPreTranslated"),
                    "provider": t.get("provider"),
                    "text": t.get("text"),
                } for t in ts],
            })
        return local

    found = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
        for res in ex.map(process, string_ids):
            found.extend(res)
    print(f"[{lang}] {len(found)} slot(s) with 2+ translations", file=sys.stderr)
    return found


def scan(session, pid, locales, strings, editor_url, max_workers):
    """Return a flat list of finding dicts, one per slot with 2+ translations."""
    string_ids = list(strings.keys())
    findings = []
    for lang in locales:
        findings.extend(scan_locale(session, pid, lang, string_ids, strings,
                                    editor_url, max_workers))
    findings.sort(key=lambda f: (f["locale"], f["identifier"] or "", str(f["pluralCategory"])))
    return findings


# --------------------------------------------------------------------------- #
# Discord reporting
# --------------------------------------------------------------------------- #
def slot_line(f):
    cat = f["pluralCategory"]
    cat_txt = f" `[{cat}]`" if cat and cat != "other" else ""
    ident = clip(f["identifier"] or f"string {f['stringId']}", 90)
    return f"• [{ident}]({f['webUrl']}){cat_txt} — **{f['count']}** translations"


def build_messages(findings, scanned_locales, note=""):
    """Return a list of Discord webhook payloads (each <=10 embeds)."""
    per_loc = collections.Counter(f["locale"] for f in findings)

    summary = {
        "title": "🈳 Crowdin: strings with multiple translations",
        "description": (
            f"Scanned **{len(scanned_locales)}** locale(s){note}. Found **{len(findings)}** "
            f"string/plural slot(s) with 2+ translations across **{len(per_loc)}** locale(s).\n"
            "Each slot needs one translation chosen as the keeper; the rest should be deleted "
            "so exactly one translation (per plural form) is exported."
        ),
        "color": 0xE67E22 if findings else 0x2ECC71,
    }
    if findings:
        top = sorted(per_loc.items(), key=lambda kv: (-kv[1], kv[0]))
        summary["fields"] = [{
            "name": "Slots per locale",
            "value": clip("\n".join(f"`{loc}` — **{n}**" for loc, n in top), 1024),
            "inline": False,
        }]

    embeds = [summary]

    if not findings:
        return pack_embeds(embeds)

    # Per-locale breakdown embeds, capped so we never exceed Discord limits.
    listed = 0
    truncated = False
    for loc, _ in sorted(per_loc.items(), key=lambda kv: (-kv[1], kv[0])):
        loc_findings = [f for f in findings if f["locale"] == loc]
        lines, desc_len = [], 0
        first_chunk = True  # first embed gets the real title; overflow ones get "(cont.)"

        def loc_title():
            return (f"{loc} — {len(loc_findings)} slot(s)" if first_chunk
                    else f"{loc} (cont.)")

        for f in loc_findings:
            if listed >= MAX_SLOTS_LISTED:
                truncated = True
                break
            line = slot_line(f)
            if desc_len + len(line) + 1 > MAX_DESC_CHARS:
                # Flush the accumulated slots as one embed and continue in a new one.
                embeds.append({"title": loc_title(), "description": "\n".join(lines),
                               "color": 0xE67E22})
                first_chunk = False
                lines, desc_len = [], 0
            lines.append(line)
            desc_len += len(line) + 1
            listed += 1
        if lines:
            embeds.append({"title": loc_title(),
                           "description": "\n".join(lines), "color": 0xE67E22})
        if truncated:
            break

    if truncated:
        embeds.append({
            "title": "…and more",
            "description": (f"Only the first **{MAX_SLOTS_LISTED}** slots are listed here. "
                            f"Run `report_multiple_translations.py --json` for the full set."),
            "color": 0x95A5A6,
        })

    return pack_embeds(embeds)


def embed_len(e):
    """Chars Discord counts toward the 6000/message budget: title + description
    + every field name/value."""
    total = len(e.get("title") or "") + len(e.get("description") or "")
    for fld in e.get("fields", []):
        total += len(fld.get("name") or "") + len(fld.get("value") or "")
    return total


def pack_embeds(embeds):
    """Split embeds into message payloads, respecting BOTH Discord's per-message
    embed count (MAX_EMBEDS_PER_MESSAGE) and cumulative char budget
    (MAX_MESSAGE_CHARS). Each individual embed already fits under the budget
    (descriptions are capped at MAX_DESC_CHARS), so this never stalls."""
    messages = []
    chunk, chunk_chars = [], 0
    for e in embeds:
        e_len = embed_len(e)
        if chunk and (len(chunk) >= MAX_EMBEDS_PER_MESSAGE
                      or chunk_chars + e_len > MAX_MESSAGE_CHARS):
            messages.append({"embeds": chunk})
            chunk, chunk_chars = [], 0
        chunk.append(e)
        chunk_chars += e_len
    if chunk:
        messages.append({"embeds": chunk})
    return messages


def post_to_discord(webhook_url, messages):
    # Use a fresh, unauthenticated session -- the Crowdin Bearer token must never
    # be sent to Discord. request_with_retry raises on any non-retryable 4xx, so
    # we translate that into the concise failure message here.
    with requests.Session() as webhook_session:
        for payload in messages:
            try:
                request_with_retry(webhook_session, "POST", webhook_url, json=payload)
            except requests.exceptions.RequestException as e:
                resp = getattr(e, "response", None)
                if resp is not None:
                    detail = f"Discord webhook failed ({resp.status_code}): {resp.text[:300]}"
                else:
                    detail = f"Discord webhook failed: {e}"
                # A rich payload can be rejected outright (e.g. an embed exceeded
                # Discord's size limits). Before crashing, best-effort post a plain
                # warning so the failure is at least visible in the channel; if even
                # that fails, fall through to the sys.exit below.
                try:
                    request_with_retry(webhook_session, "POST", webhook_url, json={
                        "content": "⚠️ Crowdin multiple-translations report failed to post "
                                   "its results (a message was rejected by Discord). "
                                   "Re-run `report_multiple_translations.py --json` for the "
                                   "full list.",
                    })
                except requests.exceptions.RequestException:
                    pass
                sys.exit(detail)


# --------------------------------------------------------------------------- #
def pick_locales(all_locales, shards, shard_index):
    """Split all_locales into `shards` deterministic, evenly-sized groups; return one.

    Locales are sorted then assigned round-robin, so each group is stable across
    runs. Scanning group `day_of_year % shards` each day covers every locale once
    per `shards`-day cycle while keeping any single run fast."""
    ordered = sorted(all_locales)
    return [l for i, l in enumerate(ordered) if i % shards == shard_index]


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("api_token", nargs="?", default=None)
    p.add_argument("--api-token", dest="api_token_opt", default=None)
    p.add_argument("--project-id", default=DEFAULT_PROJECT)
    p.add_argument("--locales", nargs="+", default=None,
                   help="scan exactly these locales (disables rotation)")
    p.add_argument("--all-locales", action="store_true",
                   help="scan every target locale in one run (~40 min; disables rotation)")
    p.add_argument("--shards", type=int, default=8,
                   help="split all target locales into this many daily shards (default 8: "
                        "~10 locales/run, full coverage every 8 days). A ~40 min full scan "
                        "does not fit a 10 min daily budget, so we rotate instead.")
    p.add_argument("--shard-index", type=int, default=None,
                   help="which shard to scan (default: today's = day-of-year %% shards)")
    p.add_argument("--webhook", help="Discord webhook URL (else DISCORD_WEBHOOK_URL).")
    p.add_argument("--max-workers", type=int, default=16,
                   help="concurrent per-string requests within a locale (default 16; Crowdin "
                        "rate-limits ~40 req/s, so higher just trades 429 retries for no gain)")
    p.add_argument("--json", dest="json_out", default=None, help="write findings to this JSON file")
    p.add_argument("--dry-run", action="store_true",
                   help="scan and print the Discord payload instead of posting it")
    args = p.parse_args()

    token = get_token(args.api_token_opt or args.api_token)
    webhook = args.webhook or os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook and not args.dry_run:
        sys.exit("No Discord webhook (pass --webhook, set DISCORD_WEBHOOK_URL, or use --dry-run).")

    pid = args.project_id
    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {token}"})

    # Project details: identifier + per-language editor codes, used to build
    # editor URLs that actually point at the right locale. (A string's own webUrl
    # always targets the first target language, regardless of the locale.)
    proj = request_with_retry(session, "GET", f"{API}/projects/{pid}").json()["data"]
    project_slug = proj["identifier"]
    src_code = proj["sourceLanguage"].get("editorCode") or proj["sourceLanguage"]["id"]
    editor_code = {l["id"]: (l.get("editorCode") or l["id"]) for l in proj["targetLanguages"]}

    # Locale selection: explicit list > all-locales > today's rotating shard.
    all_targets = proj["targetLanguageIds"]
    shard_note = ""
    if args.locales:
        locales = args.locales
    elif args.all_locales:
        locales = all_targets
    else:
        shards = max(1, args.shards)
        idx = args.shard_index if args.shard_index is not None else \
            dt.datetime.now(dt.timezone.utc).date().timetuple().tm_yday % shards
        idx %= shards
        locales = pick_locales(all_targets, shards, idx)
        shard_note = f" (rotation shard {idx + 1}/{shards})"
        print(f"Rotation: shard {idx + 1}/{shards} of {len(all_targets)} target locales",
              file=sys.stderr)

    def editor_url(lang, sid):
        return f"https://crowdin.com/editor/{project_slug}/all/{src_code}-{editor_code.get(lang, lang)}#{sid}"

    print(f"Loading strings for project {pid} …", file=sys.stderr)
    strings = {}
    for s in paged(session, f"/projects/{pid}/strings"):
        strings[s["id"]] = {"identifier": s.get("identifier"), "text": s.get("text")}
    print(f"  {len(strings)} strings; scanning {len(locales)} locale(s): "
          f"{', '.join(locales)}", file=sys.stderr)

    findings = scan(session, pid, locales, strings, editor_url, args.max_workers)

    print("\n" + "=" * 80, file=sys.stderr)
    print(f"Found {len(findings)} slot(s) with 2+ translations across "
          f"{len({f['locale'] for f in findings})} locale(s)", file=sys.stderr)
    print("=" * 80, file=sys.stderr)

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(findings, fh, indent=2, ensure_ascii=False)
        print(f"Wrote {len(findings)} findings to {args.json_out}", file=sys.stderr)

    messages = build_messages(findings, locales, shard_note)
    if args.dry_run:
        print(json.dumps(messages, indent=2, ensure_ascii=False))
        return
    post_to_discord(webhook, messages)
    print(f"Posted {len(messages)} Discord message(s).", file=sys.stderr)


if __name__ == "__main__":
    main()
