#!/usr/bin/env python3
"""
Daily Zendesk ticket triage with Claude, delivered to Discord.

Fetches open Zendesk tickets (broad query by default, not just bugs), classifies the
whole batch in one schema-enforced request, and posts a Discord summary that links
back to each original ticket and highlights the ones worth looking into (crashes,
data loss, legal requests, security/legislation, etc.).

Because this repo is public, ticket content is never written to the job summary or
anywhere public: in a normal run the only place ticket detail goes is the Discord
webhook (a private channel) and the links point at Zendesk (which needs auth to
open). Set --dry-run to print the Discord payload locally instead of posting.

The one exception is --dump-batch, a local debugging flag that writes ticket
content to a file you name. Keep those files out of the repo (see .gitignore) or
write them somewhere like /tmp.

Categories Claude sorts each ticket into:
    bug_report | low_star_review | legal_request | security_or_legislation
    | question | feature_request | other

Classification goes through the Anthropic API, with structured outputs enforcing
SCHEMA. --findings skips it entirely and renders findings produced elsewhere.

Config (env vars, or flags for local runs):
    ZENDESK_SUBDOMAIN     e.g. "mycompany"  -> https://mycompany.zendesk.com
    ZENDESK_EMAIL         agent email for API token auth
    ZENDESK_API_TOKEN     Zendesk API token
    ANTHROPIC_API_KEY     Claude API key (read by the SDK itself)
    DISCORD_WEBHOOK_URL   Discord incoming webhook (not needed with --dry-run)
    ZENDESK_QUERY         (optional) Zendesk search query; see DEFAULT_QUERY
    ZENDESK_TRIAGE_MODEL  (optional) Claude model id or alias; defaults to
                          claude-opus-5. Set it to override, e.g. `sonnet` for a
                          large backfill.

Usage:
    # real run (CI): reads everything from the environment
    python triage.py

    # local dry run: fetch + analyze, print the Discord payload, post nothing
    python triage.py --dry-run

    # what the scheduled daily run does: 48h window, skipping unchanged repeats
    python triage.py --window-hours 48 --state .triage-state/seen.json

    # or an explicit query, which overrides --window-hours
    python triage.py --query "type:ticket status:open tags:bug" --max-tickets 50

    # split classification out entirely: dump the batch, classify it by hand,
    # feed the findings back in to render
    python triage.py --dump-batch /tmp/batch.json --max-tickets 20
    python triage.py --findings /tmp/findings.json --dry-run
"""
import argparse
import json
import math
import os
import re
import sys
import textwrap
import time
from datetime import datetime, timedelta, timezone
from functools import partial

import anthropic
import requests

# Open, pending, new, and on-hold tickets, newest first. Broad on purpose: we
# want bug reports AND low-star reviews, legal requests, security/legislation
# questions, and non-English tickets — Claude does the categorising, so we don't
# filter to a single tag here.
DEFAULT_QUERY = "type:ticket status<solved order_by:created_at sort:desc"
# Whole unsolved backlog, for context in the digest. Not analyzed — just counted.
BACKLOG_QUERY = "type:ticket status<solved"
# The Search API hard-caps a query at 1000 results and returns 422 for any page past
# it (at per_page=100 that is page 11), so pagination stops here rather than walking
# into that error. Above the cap the digest reports truncation — which it already does
# for --max-tickets — instead of failing the run.
# https://developer.zendesk.com/api-reference/ticketing/ticket-management/search/#results-limit
SEARCH_RESULT_LIMIT = 1000
STATE_VERSION = 1


def build_window_query(hours):
    """Query for unsolved tickets created in the last `hours`, newest first.

    The cutoff is an explicit UTC timestamp rather than Zendesk's relative
    `created>48hours` form, so the exact window lands in the run log.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    return f"type:ticket status<solved created>{cutoff} order_by:created_at sort:desc"


def window_label(hours):
    if hours % 24 == 0 and hours >= 24:
        days = hours // 24
        return f"created in the past {days} day{'s' if days > 1 else ''}"
    return f"created in the past {hours}h"
# A pinned id rather than the `opus` alias, deliberately. This is an unattended
# digest a human skims: the batch-wide fields (`cluster`, `priority_rank`) and the
# severity calibration shift when the model underneath changes, and an alias would
# move them on someone else's release schedule. Opus rather than a cheaper tier
# because clustering asks the model to recognise one root cause across 45 tickets in
# several languages, and the whole job costs single-digit dollars a month either way.
# Bumping this is a one-line, deliberate change.
DEFAULT_MODEL = "claude-opus-5"
# Shorthands for the override, so ZENDESK_TRIAGE_MODEL=sonnet works for a big
# backfill without anyone looking up an id. The API takes ids only, so they are
# mapped here; each is the newest model in its family, and a full id passes through
# untouched.
API_MODEL_ALIASES = {
    "opus": "claude-opus-5",
    "sonnet": "claude-sonnet-5",
    "haiku": "claude-haiku-4-5",
}
DEFAULT_MAX_TICKETS = 100
DESCRIPTION_CHARS = 1500  # per-ticket description sent to Claude (triage only)
# One classification runs ~100 output tokens per ticket, and adaptive thinking draws
# from the same output budget. 400 keeps a chunk far under the model's 128K output
# ceiling; batches larger than this are split rather than truncated.
DEFAULT_BATCH_SIZE = 400
MAX_OUTPUT_TOKENS = 128000

# ---- Taxonomy --------------------------------------------------------------
#
# Single source of truth. The schema enum, the Discord labels, the urgency colours,
# and the system-prompt guidance are all derived from this table, so adding a
# category is one edit and the model can never be given an enum value that the
# prompt never explains.
#
# Percentages come from a 3,662-ticket sample of the 13 months to 2026-08.
# Columns: (name, Discord label, urgent, guidance for the model)
CATEGORY_SPECS = (
    ("abuse_report", "🚨 Abuse report", True,
     "One user reporting another account for illegal or abusive content (CSAM, "
     "harassment, drugs, impersonation). Usually quotes the offending Session ID. "
     "~11% of non-review tickets. Always set worth_looking_into."),
    ("security_report", "🔒 Security report", True,
     "A vulnerability, exploit, or account-compromise disclosure. Not the same as a "
     "policy question. Always set worth_looking_into."),
    ("legal_or_data_request", "⚖️ Legal / data request", True,
     "GDPR or data-deletion request, subpoena, law-enforcement or court order. "
     "Always set worth_looking_into."),
    ("bug_report", "🐞 Bug report", False,
     "Something in the app is broken or misbehaving."),
    ("account_access", "🔑 Account access", False,
     "Lost recovery phrase, locked out, or asking to restore an account. Usually "
     "irreversible by design, but track the volume."),
    ("policy_question", "📜 Policy question", False,
     "Questions about law, regulation, or policy — 'Chat Control', encryption "
     "backdoors, whether Session complies with something."),
    ("low_star_review", "⭐ Low-star review", False,
     "An app-store review of 3 stars or fewer. These often hide a real bug — put "
     "the underlying problem in `summary`."),
    ("positive_review", "👍 Positive review", False,
     "An app-store review of 4-5 stars with no actionable content."),
    ("feature_request", "💡 Feature request", False,
     "Asking for something the app does not do yet."),
    ("question", "❓ Question", False,
     "A how-do-I or usage question that is not a bug."),
    ("spam_or_solicitation", "🗑️ Spam / solicitation", False,
     "Marketing, token or OTC investment offers, partnership pitches, listing spam."),
    ("other", "• Other", False,
     "Genuinely none of the above. Prefer a specific category wherever one fits."),
)
CATEGORIES = [name for name, _, _, _ in CATEGORY_SPECS]
CATEGORY_LABEL = {name: label for name, label, _, _ in CATEGORY_SPECS}
# Categories whose urgency `severity` cannot express. They are not bugs, so the model
# rates them not_applicable — which would otherwise give the most serious ticket in
# the batch the calmest marker and sort it last.
# The emoji on its own, for the per-ticket lines. Derived from the label so the
# table stays the single place a category is described.
CATEGORY_EMOJI = {name: label.split(" ", 1)[0] for name, label, _, _ in CATEGORY_SPECS}
URGENT_CATEGORIES = frozenset(name for name, _, urgent, _ in CATEGORY_SPECS if urgent)
CATEGORY_GUIDANCE = "\n".join(f"- {name}: {desc}" for name, _, _, desc in CATEGORY_SPECS)

SEVERITIES = ["crash", "data_loss", "major", "minor", "cosmetic", "not_applicable"]
PLATFORMS = [
    "ios", "android", "desktop_windows", "desktop_macos", "desktop_linux",
    "multiple", "unknown",
]

# Structured-output schema. Kept within the structured-output constraints:
# additionalProperties:false everywhere, every field required, enums for the
# closed sets, no min/max-length constraints.
TICKET_PROPERTIES = {
    "id": {"type": "integer", "description": "The Zendesk ticket id, echoed back unchanged."},
    "category": {"type": "string", "enum": CATEGORIES},
    "severity": {
        "type": "string",
        "enum": SEVERITIES,
        "description": "crash/data_loss are the most serious; not_applicable for non-bug tickets.",
    },
    "affected_component": {
        "type": "string",
        "description": "Best guess at the product area, e.g. 'onboarding', 'attachments', 'sync'. Empty string if unknown.",
    },
    "summary": {
        "type": "string",
        "description": "One line stating what is actually broken or what the user actually wants.",
    },
    "likely_root_cause": {
        "type": "string",
        "description": "Short hypothesis for the underlying cause. Empty string if not a bug or unclear.",
    },
    "language": {
        "type": "string",
        "description": "Language the ticket is written in, e.g. 'English', 'German'.",
    },
    "priority_rank": {
        "type": "integer",
        "description": "Relative triage priority, 1 = look at first.",
    },
    "worth_looking_into": {
        "type": "boolean",
        "description": "True if a human should review this soon. Always true for abuse_report, security_report, and legal_or_data_request.",
    },
    "cluster": {
        "type": "string",
        "description": "Short label grouping tickets with the same likely root cause; identical labels mean likely duplicates/clusters. Empty string if standalone.",
    },
    "platform": {
        "type": "string",
        "enum": PLATFORMS,
        "description": "Platform the ticket is about. 'multiple' if several, 'unknown' if not stated.",
    },
    "app_version": {
        "type": "string",
        "description": "App version if the ticket states one, e.g. '2.15.2'. Empty string otherwise.",
    },
    "reported_session_id": {
        "type": "string",
        "description": "For abuse reports: the reported account's Session ID (66 hex chars, starts with 05), copied exactly. Empty string if the ticket gives none.",
    },
}
SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["tickets"],
    "properties": {
        "tickets": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": list(TICKET_PROPERTIES.keys()),
                "properties": TICKET_PROPERTIES,
            },
        }
    },
}

_SYSTEM_PROMPT_TEMPLATE = textwrap.dedent(
    """
    You are a senior support-triage engineer. You are given a batch of Zendesk
    support tickets as JSON. For every ticket, return a classification object.

    Tickets arrive in many languages and many are machine-imported app-store
    reviews. Classify what the user is actually reporting, not how they said it.

    Categories — use exactly these values:
    __CATEGORIES__

    Guidelines:
    - Infer severity from the description: crash and data_loss are the most
      serious; use not_applicable for tickets that are not bug reports.
    - Group tickets that share a likely root cause under the same short `cluster`
      label, even if worded differently or in different languages. Identical
      labels flag likely duplicates/clusters.
    - `summary` is one plain line: what is actually broken, or what the user
      actually wants. Do not restate the ticket subject.
    - Mark `worth_looking_into` true for anything a human should see soon:
      crashes, data loss, major bugs, and every abuse_report, security_report,
      and legal_or_data_request. Be selective otherwise — not everything is major.
    - For abuse_report, copy the reported account's Session ID into
      `reported_session_id` exactly as written. Do not invent or reformat it.
    - Rank by `priority_rank` (1 = first) across the whole batch.
    - Echo each ticket `id` back exactly. Return one object per input ticket.
    """
).strip()
# Substituted after dedent so the guidance block keeps its own formatting.
SYSTEM_PROMPT = _SYSTEM_PROMPT_TEMPLATE.replace("__CATEGORIES__", CATEGORY_GUIDANCE)


def get_env(name, cli_value=None, required=True):
    if cli_value:
        return cli_value
    value = os.environ.get(name)
    if value:
        return value
    if required:
        sys.exit(f"Missing required config: set the {name} environment variable (or pass the matching flag).")
    return None


def zendesk_session(email, token):
    session = requests.Session()
    # Zendesk API-token auth: username is "{email}/token", password is the token.
    session.auth = (f"{email}/token", token)
    session.headers["Accept"] = "application/json"
    return session


def retry_after_seconds(resp, default):
    """Seconds to wait per the Retry-After header, falling back to `default`.

    RFC 9110 allows either a delay in seconds or an HTTP-date; float() on the date
    form raises, so anything unparseable falls back rather than crashing the run.

    Negative, NaN, and infinite values fall back too: time.sleep() rejects the first
    two outright, so a hostile or buggy proxy sending `Retry-After: -30` would
    otherwise take the run down with a ValueError.
    """
    raw = resp.headers.get("retry-after")
    if raw is None:
        return default
    try:
        seconds = float(raw)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(seconds) or seconds < 0:
        return default
    return seconds


def request_with_retry(session, method, url, attempts=6, **kwargs):
    """GET/POST with backoff on 429 and 5xx.

    Lower `attempts` for calls whose result is nice-to-have: the full budget can
    burn ~60s of backoff, which is not worth spending on optional data.
    """
    if attempts <= 0:
        raise ValueError("attempts must be at least 1")

    delay = 1.0
    last_exc = None
    resp = None
    for attempt in range(attempts):
        final = attempt == attempts - 1
        try:
            resp = session.request(method, url, timeout=30, **kwargs)
        except requests.RequestException as exc:
            last_exc = exc
            if final:
                break
            time.sleep(delay)
            delay = min(delay * 2, 30)
            continue
        if resp.status_code == 429 or resp.status_code >= 500:
            if final:
                break
            time.sleep(min(retry_after_seconds(resp, delay), 60))
            delay = min(delay * 2, 30)
            continue
        return resp
    if last_exc:
        raise last_exc
    return resp


def fetch_tickets(session, subdomain, query, max_tickets):
    """Fetch tickets via the Zendesk Search API, following pagination.

    Returns (tickets, total_matched). total_matched is the full result count
    reported by Zendesk, which can exceed len(tickets) when max_tickets — or
    SEARCH_RESULT_LIMIT — caps the batch; the caller surfaces that gap so the
    truncation isn't silent.
    """
    base = f"https://{subdomain}.zendesk.com/api/v2/search.json"
    url = base
    params = {"query": query, "per_page": 100}
    tickets = []
    total_matched = None
    # Whichever bites first: our own runaway guard or Zendesk's hard result limit.
    cap = min(max_tickets, SEARCH_RESULT_LIMIT)
    while url and len(tickets) < cap:
        resp = request_with_retry(session, "GET", url, params=params)
        params = None  # next_page already carries the query
        if resp.status_code == 403:
            sys.exit("Zendesk returned 403 — the API token/email may lack search access.")
        # 422 past the result limit: `cap` should have stopped us first, so this only
        # fires if the account's effective limit is lower than documented. Keep the
        # tickets already in hand — a partial digest beats no digest — and let the
        # caller report the gap. With nothing in hand there is nothing to salvage.
        if resp.status_code == 422 and tickets:
            print(f"Note: Zendesk stopped paginating at {len(tickets)} results "
                  f"(search result limit); analyzing what was fetched.")
            break
        if resp.status_code >= 400:
            sys.exit(f"Zendesk search failed ({resp.status_code}): {resp.text[:300]}")
        payload = resp.json()
        if total_matched is None:
            total_matched = payload.get("count")
        for row in payload.get("results", []):
            if row.get("result_type") != "ticket":
                continue
            tickets.append(row)
            if len(tickets) >= cap:
                break
        url = payload.get("next_page")
    return tickets, total_matched


def fetch_total_unsolved(session, subdomain):
    """Count the whole unsolved backlog. Best effort: returns None on failure.

    Context for the digest, not something to hold the run up for — hence the
    short retry budget.
    """
    url = f"https://{subdomain}.zendesk.com/api/v2/search/count.json"
    try:
        resp = request_with_retry(
            session, "GET", url, attempts=2, params={"query": BACKLOG_QUERY}
        )
        if resp.status_code >= 400:
            print(f"Note: could not count the unsolved backlog ({resp.status_code}).")
            return None
        return resp.json().get("count")
    except (requests.RequestException, ValueError) as exc:
        # request_with_retry re-raises the transport error once its (short) budget is
        # spent, and .json() raises on a non-JSON body — neither is a reason to lose
        # the digest over one context number, so both land on the documented None.
        print(f"Note: could not count the unsolved backlog ({exc}).")
        return None


# ---- Dedup state -----------------------------------------------------------
#
# Maps ticket id -> {updated_at, last_reported}. A ticket is re-reported only if
# Zendesk's updated_at has moved since we last showed it, so the daily 48h window
# does not repost yesterday's unchanged tickets. The state lives outside the repo
# (CI restores it from the Actions cache), so every read degrades gracefully: a
# missing or corrupt file just means everything looks new.


def empty_state():
    return {"version": STATE_VERSION, "seen": {}}


def load_state(path):
    if not os.path.exists(path):
        print(f"No state file at {path}; treating every ticket in the window as new.")
        return empty_state()
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Note: unreadable state file {path} ({exc}); treating every ticket as new.")
        return empty_state()
    if not isinstance(data, dict) or not isinstance(data.get("seen"), dict):
        print(f"Note: unexpected shape in {path}; treating every ticket as new.")
        return empty_state()
    # A state file written by a different schema version can't be trusted field by
    # field, so treat it as a cache miss rather than misreading it.
    if data.get("version") != STATE_VERSION:
        print(f"Note: {path} is version {data.get('version')!r}, expected {STATE_VERSION}; "
              f"treating every ticket as new.")
        return empty_state()
    print(f"Loaded state for {len(data['seen'])} previously reported tickets.")
    return data


def partition_by_state(tickets, state):
    """Split into (new, changed, unchanged) against saved state.

    `changed` means Zendesk's updated_at differs from what we recorded — note that
    any agent action (reply, tag, status change) bumps updated_at, not just an
    end-user comment.
    """
    seen = state.get("seen", {})
    new, changed, unchanged = [], [], []
    for ticket in tickets:
        previous = seen.get(str(ticket.get("id")))
        if previous is None:
            new.append(ticket)
        elif previous.get("updated_at") != ticket.get("updated_at"):
            changed.append(ticket)
        else:
            unchanged.append(ticket)
    return new, changed, unchanged


def save_state(path, state, reported, retention_days):
    """Record `reported` as seen, prune old entries, write atomically.

    Returns (kept, pruned).
    """
    now = datetime.now(timezone.utc)
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    seen = dict(state.get("seen", {}))
    for ticket in reported:
        seen[str(ticket.get("id"))] = {
            "updated_at": ticket.get("updated_at"),
            "last_reported": stamp,
        }

    # Bound the file: the window is 48h, so anything older than retention is moot.
    cutoff = now - timedelta(days=retention_days)
    kept = {}
    for ticket_id, record in seen.items():
        try:
            last = datetime.strptime(
                record.get("last_reported", ""), "%Y-%m-%dT%H:%M:%SZ"
            ).replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            continue  # malformed entry — drop it rather than keep it forever
        if last >= cutoff:
            kept[ticket_id] = record

    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    temporary = f"{path}.tmp"
    with open(temporary, "w", encoding="utf-8") as fh:
        json.dump({"version": STATE_VERSION, "updated_at": stamp, "seen": kept}, fh, indent=2)
    os.replace(temporary, path)  # atomic: a crash mid-write can't corrupt the state
    return len(kept), len(seen) - len(kept)


# ---- App-store review filtering --------------------------------------------
#
# AppFollow imports app-store reviews into Zendesk through this channel. In the
# 13-month sample it identified reviews with no false positives (2,656 of 2,656),
# whereas the `app-store` tag was present on only 287 of them — so filter on the
# channel, not on tags. 4-5 star reviews were 59% of *all* tickets and are never
# actionable, so counting them beats paying tokens to classify them.
REVIEW_CHANNEL = "any_channel"
STAR_SUBJECT = re.compile(r"^\s*([★☆]{1,10})")
DEFAULT_REVIEW_STAR_FLOOR = 3


def squash(value):
    """Collapse whitespace so subject/description can be compared meaningfully."""
    return re.sub(r"\s+", " ", value or "").strip()


def review_stars(ticket):
    """Star count from an AppFollow review subject, or None if not a review subject."""
    match = STAR_SUBJECT.match(ticket.get("subject") or "")
    return match.group(1).count("★") if match else None


def is_store_review(ticket):
    return (((ticket.get("via") or {}).get("channel") == REVIEW_CHANNEL)
            or STAR_SUBJECT.match(ticket.get("subject") or "") is not None)


def partition_reviews(tickets, star_floor):
    """Split off app-store reviews rated above `star_floor`.

    Reviews whose rating cannot be parsed are kept: spending a few tokens beats
    dropping a real complaint.
    """
    keep, skipped = [], []
    for ticket in tickets:
        stars = review_stars(ticket)
        if is_store_review(ticket) and stars is not None and stars > star_floor:
            skipped.append(ticket)
        else:
            keep.append(ticket)
    return keep, skipped


def is_content_free(ticket):
    """True when the description just repeats the subject, carrying no information.

    Twitter DM tickets arrive this way — subject and body are both
    "Conversation with <handle>" — 15% of non-review tickets in the sample. Sent
    as-is they are unclassifiable, so the model invents a category for a handle.
    """
    return squash(ticket.get("description")) == squash(ticket.get("subject"))


def hydrate_descriptions(session, subdomain, tickets):
    """Fill content-free descriptions from the ticket's comments. Returns the count.

    Costs one extra API call per affected ticket, so it only runs for those.
    """
    hydrated = 0
    for ticket in tickets:
        if not is_content_free(ticket):
            continue
        url = f"https://{subdomain}.zendesk.com/api/v2/tickets/{ticket['id']}/comments.json"
        try:
            resp = request_with_retry(session, "GET", url, attempts=2, params={"per_page": 10})
        except requests.RequestException as exc:
            # Hydration is an enrichment, never a reason to abort the digest: an
            # unreachable comments endpoint just leaves the description as-is.
            print(f"Note: could not fetch comments for #{ticket['id']} ({exc}).")
            continue
        if resp.status_code >= 400:
            continue
        try:
            comments = resp.json().get("comments", [])
        except ValueError as exc:
            # A 200 carrying an HTML error page (proxy, maintenance) is the same kind
            # of non-event as an HTTP error here — enrich what we can, skip the rest.
            print(f"Note: unreadable comments payload for #{ticket['id']} ({exc}).")
            continue
        subject = squash(ticket.get("subject"))
        bodies = [squash(c.get("body")) for c in comments]
        useful = [b for b in bodies if b and b != subject]
        if useful:
            ticket["description"] = "\n".join(useful)
            hydrated += 1
    if hydrated:
        print(f"Recovered {hydrated} content-free description(s) from ticket comments.")
    return hydrated


def compact_ticket(ticket):
    """Reduce a Zendesk ticket to the fields Claude needs for triage."""
    description = (ticket.get("description") or "").strip()
    truncated = len(description) > DESCRIPTION_CHARS
    if truncated:
        description = description[:DESCRIPTION_CHARS] + " …[truncated]"
    rating = ticket.get("satisfaction_rating") or {}
    return {
        "id": ticket.get("id"),
        "subject": ticket.get("subject") or "",
        "description": description,
        "tags": ticket.get("tags") or [],
        "priority": ticket.get("priority"),
        "status": ticket.get("status"),
        "satisfaction_rating": rating.get("score"),
    }


def resolve_api_model(model):
    """Map a shorthand model name onto the id the Anthropic API expects.

    Anything that isn't a known shorthand passes through untouched, so a pinned id
    (`claude-opus-4-8`) or a model newer than this table still works.
    """
    return API_MODEL_ALIASES.get(model, model)


def build_analysis_prompt(compact_tickets):
    return (
        "Classify every ticket in this batch and return one object per ticket.\n\n"
        "TICKETS (JSON):\n"
        + json.dumps(compact_tickets, ensure_ascii=False)
    )


REQUIRED_FINDING_KEYS = ("id", "category", "severity")


def validate_findings(findings, label):
    """Exit unless every entry is an object carrying the keys the renderer indexes.

    build_header does f["category"] / f["severity"] and build_ticket_line does
    f["id"], so a missing key surfaces as a KeyError halfway through building a
    Discord payload. Failing here names the offending entry instead.
    """
    for position, entry in enumerate(findings):
        if not isinstance(entry, dict):
            sys.exit(f"{label}: entry {position} is {type(entry).__name__}, expected an object.")
        missing = [key for key in REQUIRED_FINDING_KEYS if key not in entry]
        if missing:
            sys.exit(f"{label}: entry {position} (id={entry.get('id')!r}) is missing "
                     f"required key(s): {', '.join(missing)}.")
    return findings


def tickets_from_payload(payload, source):
    """Pull the `tickets` list out of a classification payload, or exit clearly.

    Structured outputs guarantee the key and the item shape, so on a normal run this
    never fires; it is the guard for --findings, whose contents nothing validates,
    and a bare KeyError mid-render is a confusing way to learn a key is missing.
    """
    found = payload.get("tickets") if isinstance(payload, dict) else None
    if not isinstance(found, list):
        shape = sorted(payload) if isinstance(payload, dict) else type(payload).__name__
        sys.exit(f"{source} returned no 'tickets' list (got: {shape}).")
    return validate_findings(found, source)


def dump_batch(path, compact_tickets, model):
    """Write the batch to disk so it can be classified by hand. Contains ticket text."""
    payload = {
        "model": model,
        "system_prompt": SYSTEM_PROMPT,
        "schema": SCHEMA,
        "tickets": compact_tickets,
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)


def load_findings(path):
    """Read findings written by hand or by another tool: {"tickets": [...]} or [...].

    Validates the keys the Discord renderer indexes directly, so a hand-edited file
    fails here with the offending entry rather than as a KeyError mid-render.
    """
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    findings = data.get("tickets") if isinstance(data, dict) else data
    if not isinstance(findings, list):
        sys.exit(f"{path}: expected a JSON list, or an object with a 'tickets' list.")
    return validate_findings(findings, path)


def analyze_in_chunks(analyzer, compact_tickets, batch_size):
    """Classify in chunks so a big batch can't blow the output token ceiling.

    Chunking is per-request, so `cluster` labels and `priority_rank` are only
    meaningful within a chunk — batches that actually split are large enough that
    cross-chunk cluster fidelity is secondary to completing at all.
    """
    if len(compact_tickets) <= batch_size:
        return analyzer(compact_tickets)

    findings = []
    chunks = (len(compact_tickets) + batch_size - 1) // batch_size
    print(f"Batch of {len(compact_tickets)} exceeds {batch_size}; splitting into {chunks} requests.")
    for number, start in enumerate(range(0, len(compact_tickets), batch_size), start=1):
        chunk = compact_tickets[start : start + batch_size]
        print(f"  chunk {number}/{chunks}: {len(chunk)} tickets")
        findings.extend(analyzer(chunk))
    return findings


def analyze(client, model, effort, compact_tickets):
    prompt = build_analysis_prompt(compact_tickets)
    with client.messages.stream(
        model=model,
        max_tokens=MAX_OUTPUT_TOKENS,
        thinking={"type": "adaptive"},
        output_config={
            "format": {"type": "json_schema", "schema": SCHEMA},
            "effort": effort,
        },
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        message = stream.get_final_message()

    if message.stop_reason == "refusal":
        sys.exit("Claude refused to process the batch.")
    if message.stop_reason == "max_tokens":
        # Structured output truncated mid-JSON: json.loads below would fail with a
        # baffling parse error, so say what actually went wrong.
        sys.exit(
            f"Claude hit the {MAX_OUTPUT_TOKENS} output-token limit on a batch of "
            f"{len(compact_tickets)} tickets, so the JSON is incomplete. "
            f"Lower --batch-size (currently splitting at {DEFAULT_BATCH_SIZE})."
        )
    text = next((b.text for b in message.content if b.type == "text"), None)
    if not text:
        sys.exit("Claude returned no structured output.")
    return tickets_from_payload(json.loads(text), "Claude")


# ---- Discord rendering -----------------------------------------------------
#
# The digest is plain message text: a short header, then one line per ticket. Its job
# is to be skimmed, and a labelled box per field reads as a wall at 16 tickets a day.

# Leads each line so severity is scannable straight down the left edge. Urgent
# categories get URGENT_MARKER instead: they are not bugs, so the model rates them
# not_applicable, and the calmest marker on the most serious ticket is backwards.
SEVERITY_EMOJI = {
    "crash": "🔥",
    "data_loss": "💥",
    "major": "🟠",
    "minor": "🟡",
    "cosmetic": "⚪",
    "not_applicable": "▫️",
}
URGENT_MARKER = "🚨"
# Discord's cap on one message's content. Lines are clipped and chunked against it.
MAX_MESSAGE_CHARS = 2000
SUMMARY_CHARS = 160
ROOT_CAUSE_CHARS = 140
MAX_HIGHLIGHTS = 27  # ~3 messages' worth of lines, plus the header


def ticket_url(subdomain, ticket_id):
    return f"https://{subdomain}.zendesk.com/agent/tickets/{ticket_id}"


def clip(text, limit):
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def is_urgent(finding):
    return finding.get("category") in URGENT_CATEGORIES


def severity_marker(finding):
    """The leading emoji: category urgency first, then severity."""
    if is_urgent(finding):
        return URGENT_MARKER
    return SEVERITY_EMOJI.get(finding.get("severity", "not_applicable"), "▫️")


def build_ticket_line(finding, subdomain, is_update=False):
    """One skimmable line per ticket:

        🔥 | 🐞 | #27605 · Notifications only arrive… | Likely cause: push service…

    The id is a masked link, so the ticket stays one click away without spending the
    character budget on a visible URL.
    """
    tid = finding["id"]
    # 🔄 marks a ticket already shown that has since changed, so the reader knows it
    # is a follow-up rather than a duplicate post.
    marker = "🔄 " if is_update else ""
    parts = [
        severity_marker(finding),
        CATEGORY_EMOJI.get(finding.get("category"), "•"),
        f"{marker}[#{tid}]({ticket_url(subdomain, tid)}) · "
        f"{clip(finding.get('summary'), SUMMARY_CHARS) or '(no summary)'}",
    ]
    root = clip(finding.get("likely_root_cause"), ROOT_CAUSE_CHARS)
    if root:
        parts.append(f"Likely cause: {root}")
    # The reported account is the actionable part of an abuse report — carrying it on
    # the line saves opening the ticket to copy it.
    reported = clip(finding.get("reported_session_id"), 70)
    if reported:
        parts.append(f"Reported: `{reported}`")
    return " | ".join(parts)


def build_header(findings, highlights, stats=None):
    """The lead lines: what was looked at, the category tally, duplicate clusters.

    Accounts for the batch honestly — how much of the window was analyzed, what was
    skipped and why, how big the untriaged backlog behind it is — so a short digest
    never reads as a quiet day when it was really a truncated one.
    """
    by_category = {}
    by_severity = {}
    clusters = {}
    for f in findings:
        by_category[f["category"]] = by_category.get(f["category"], 0) + 1
        by_severity[f["severity"]] = by_severity.get(f["severity"], 0) + 1
        label = (f.get("cluster") or "").strip()
        if label:
            clusters.setdefault(label, []).append(f["id"])

    stats = stats or {}
    matched = stats.get("matched")
    skipped = stats.get("skipped_unchanged") or 0
    updated = stats.get("updated_count") or 0
    backlog = stats.get("total_unsolved")

    window = f"🗂️ **Zendesk triage** — analyzed **{len(findings)}**"
    if matched is not None:
        window += f" of **{matched}**"
    window += " tickets in the window"
    if stats.get("scope"):
        window += f" ({stats['scope']})"
    window += "."
    if skipped:
        window += f" Skipped **{skipped}** already reported and unchanged."
    reviews = stats.get("skipped_reviews") or 0
    if reviews:
        window += f" Skipped **{reviews}** positive app-store review(s)."
    lines = [window]

    if backlog is not None:
        lines.append(f"Backlog: **{backlog:,}** unsolved tickets in total (not triaged).")

    serious = by_severity.get("crash", 0) + by_severity.get("data_loss", 0)
    tail = f"**{len(highlights)}** worth looking into"
    tail += f", including **{serious}** crash/data-loss." if serious else "."
    if updated:
        tail += f" 🔄 **{updated}** changed since last reported."
    lines.append(tail)

    if by_category:
        lines.append(clip(" · ".join(
            f"{CATEGORY_EMOJI.get(cat, '•')} **{count}**"
            for cat, count in sorted(by_category.items(), key=lambda kv: -kv[1])
        ), 300))

    dup_clusters = {k: v for k, v in clusters.items() if len(v) > 1}
    if dup_clusters:
        cluster_lines = " · ".join(
            f"**{clip(label, 40)}** ×{len(ids)} (#{', #'.join(str(i) for i in ids[:4])})"
            for label, ids in sorted(dup_clusters.items(), key=lambda kv: -len(kv[1]))[:4]
        )
        lines.append(f"Likely duplicates: {clip(cluster_lines, 400)}")

    return "\n".join(lines)


def chunk_entries(entries):
    """Group (line, ticket_ids) pairs into messages within MAX_MESSAGE_CHARS.

    Lines are joined with a newline, so each one after the first costs a character
    more than its own length. An entry longer than the cap still gets its own message
    rather than being dropped; the pieces are pre-clipped so that shouldn't arise.
    """
    chunks, current, current_chars = [], [], 0
    for text, ids in entries:
        projected = current_chars + len(text) + (1 if current else 0)
        if current and projected > MAX_MESSAGE_CHARS:
            chunks.append(current)
            current, current_chars = [], 0
            projected = len(text)
        current.append((text, ids))
        current_chars = projected
    if current:
        chunks.append(current)
    return chunks


def select_highlights(findings):
    """Ordered highlights split into (shown, omitted) by the display cap.

    Urgent categories are included even if the model failed to flag them, and sort
    ahead of everything else — an abuse or legal report must not be pushed out of the
    digest by a queue of ordinary bugs. Shared with state recording so the display
    and what gets marked reported can't drift apart.
    """
    highlights = [f for f in findings if f.get("worth_looking_into") or is_urgent(f)]
    highlights.sort(key=lambda f: (not is_urgent(f), f.get("priority_rank", 9999)))
    return highlights[:MAX_HIGHLIGHTS], highlights[MAX_HIGHLIGHTS:]


def build_messages(findings, subdomain, stats=None, updated_ids=None):
    """Return (messages, coverage).

    coverage[i] is the set of ticket ids message i accounts for, so a partial post
    failure can still record exactly the tickets that reached Discord.
    """
    updated_ids = updated_ids or set()
    shown, omitted = select_highlights(findings)
    shown_ids = {f.get("id") for f in shown}
    omitted_ids = {f.get("id") for f in omitted}

    # The header accounts for every classified ticket except the highlights that
    # didn't fit; those are covered by no message and stay eligible next run.
    header_ids = {f.get("id") for f in findings} - shown_ids - omitted_ids
    header = build_header(findings, shown + omitted, stats)
    if omitted:
        header += (f"\nShowing the top **{len(shown)}** of "
                   f"**{len(shown) + len(omitted)}** worth looking into.")
    entries = [(header, header_ids)]
    entries += [
        (build_ticket_line(f, subdomain, is_update=f.get("id") in updated_ids),
         {f.get("id")})
        for f in shown
    ]

    messages, coverage = [], []
    for chunk in chunk_entries(entries):
        messages.append({"content": "\n".join(text for text, _ in chunk)})
        covered = set()
        for _, ids in chunk:
            covered |= ids
        coverage.append(covered)
    return messages, coverage


def post_to_discord(session, webhook_url, messages):
    """POST each message in order; return how many Discord accepted.

    Stops at the first failure and returns the accepted count instead of exiting, so
    the caller can record the tickets that did land before signalling the failure —
    otherwise a failure on message 3 of 3 reposts messages 1 and 2 on the next run.
    """
    for index, payload in enumerate(messages):
        resp = request_with_retry(session, "POST", webhook_url, json=payload)
        if resp.status_code >= 400:
            print(f"Discord webhook failed on message {index + 1}/{len(messages)} "
                  f"({resp.status_code}): {resp.text[:300]}")
            return index
    return len(messages)


def main():
    parser = argparse.ArgumentParser(description="Triage open Zendesk tickets with Claude and post a Discord summary.")
    parser.add_argument("--subdomain", help="Zendesk subdomain (else ZENDESK_SUBDOMAIN).")
    parser.add_argument("--email", help="Zendesk agent email (else ZENDESK_EMAIL).")
    parser.add_argument("--api-token", help="Zendesk API token (else ZENDESK_API_TOKEN).")
    parser.add_argument("--webhook", help="Discord webhook URL (else DISCORD_WEBHOOK_URL).")
    parser.add_argument("--query", help="Zendesk search query (else ZENDESK_QUERY, else default). "
                                        "Takes precedence over --window-hours.")
    parser.add_argument("--window-hours", type=int, metavar="N",
                        help="Only analyze unsolved tickets created in the last N hours. "
                             "The scheduled daily run uses 48.")
    parser.add_argument("--model", help=f"Claude model id, or an alias (opus, sonnet, "
                                        f"haiku) mapped to an id "
                                        f"(else ZENDESK_TRIAGE_MODEL, else {DEFAULT_MODEL}).")
    parser.add_argument("--effort", default="medium", choices=["low", "medium", "high", "xhigh", "max"],
                        help="Claude reasoning effort (default: medium).")
    parser.add_argument("--max-tickets", type=int, default=DEFAULT_MAX_TICKETS,
                        help=f"Max tickets to analyze (default: {DEFAULT_MAX_TICKETS}).")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE, metavar="N",
                        help=f"Split batches larger than N tickets across multiple requests, "
                             f"so a big batch can't exceed the output token ceiling "
                             f"(default: {DEFAULT_BATCH_SIZE}).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Fetch and analyze, then print the Discord payload instead of posting.")
    parser.add_argument("--findings", metavar="PATH",
                        help="Render findings classified elsewhere, skipping Zendesk and "
                             "Claude entirely. Pairs with --dump-batch.")
    parser.add_argument("--review-star-floor", type=int, default=DEFAULT_REVIEW_STAR_FLOOR,
                        metavar="N",
                        help=f"Classify app-store reviews of N stars or fewer; count the rest "
                             f"without spending tokens (default: {DEFAULT_REVIEW_STAR_FLOOR}).")
    parser.add_argument("--include-positive-reviews", action="store_true",
                        help="Classify every app-store review, including 4-5 star ones.")
    parser.add_argument("--no-hydrate", action="store_true",
                        help="Skip fetching comments for tickets whose description just "
                             "repeats the subject (e.g. Twitter DMs).")
    parser.add_argument("--state", metavar="PATH",
                        help="Dedup state file. When set, tickets already reported and "
                             "unchanged (same Zendesk updated_at) are skipped entirely; "
                             "changed ones are re-reported and flagged. Written only on a "
                             "real run, after Discord accepts the post.")
    parser.add_argument("--state-retention-days", type=int, default=30, metavar="N",
                        help="Forget state entries older than N days (default: 30).")
    parser.add_argument("--dump-batch", metavar="PATH",
                        help="Write the batch (tickets, prompt, schema) to PATH and exit, for "
                             "hand-classification. WARNING: writes ticket content to disk.")
    args = parser.parse_args()

    # Subdomain is always needed: it builds the ticket links in the Discord payload.
    subdomain = get_env("ZENDESK_SUBDOMAIN", args.subdomain)
    # A dump exits before rendering anything, so it never needs the webhook either.
    needs_webhook = not (args.dry_run or args.dump_batch)
    webhook = get_env("DISCORD_WEBHOOK_URL", args.webhook, required=needs_webhook)
    model = args.model or os.environ.get("ZENDESK_TRIAGE_MODEL") or DEFAULT_MODEL

    stats = {}
    state = None
    classified = []
    updated_ids = set()

    if args.findings:
        # Findings already exist, so neither Zendesk nor a model is involved.
        findings = load_findings(args.findings)
        print(f"Loaded {len(findings)} findings from {args.findings}.")
    else:
        email = get_env("ZENDESK_EMAIL", args.email)
        api_token = get_env("ZENDESK_API_TOKEN", args.api_token)

        # An explicit query wins over --window-hours; warn rather than silently drop it.
        explicit_query = args.query or os.environ.get("ZENDESK_QUERY")
        if explicit_query:
            if args.window_hours:
                print("Note: --window-hours ignored because an explicit query was given.")
            query = explicit_query
        elif args.window_hours:
            query = build_window_query(args.window_hours)
            stats["scope"] = window_label(args.window_hours)
        else:
            query = DEFAULT_QUERY

        zd = zendesk_session(email, api_token)
        tickets, total_matched = fetch_tickets(zd, subdomain, query, args.max_tickets)
        matched = "?" if total_matched is None else total_matched
        print(f"Fetched {len(tickets)} of {matched} matching tickets (query: {query!r}).")
        if total_matched is not None and total_matched > len(tickets):
            # Name whichever cap actually bound, so a truncated digest doesn't send
            # someone raising --max-tickets against a limit that isn't ours.
            reason = (f"Zendesk's search API returns at most {SEARCH_RESULT_LIMIT} results"
                      if args.max_tickets >= SEARCH_RESULT_LIMIT
                      else f"--max-tickets is {args.max_tickets}")
            print(f"Note: {total_matched - len(tickets)} matching tickets were not analyzed "
                  f"({reason}).")
        if not tickets:
            print("No tickets matched the query; nothing to do.")
            return

        stats["matched"] = total_matched
        stats["total_unsolved"] = fetch_total_unsolved(zd, subdomain)

        # Drop positive store reviews before anything expensive: they were 59% of all
        # tickets in the sample and never actionable.
        if not args.include_positive_reviews:
            tickets, skipped_reviews = partition_reviews(tickets, args.review_star_floor)
            if skipped_reviews:
                stats["skipped_reviews"] = len(skipped_reviews)
                print(f"Skipped {len(skipped_reviews)} app-store review(s) above "
                      f"{args.review_star_floor} stars; {len(tickets)} tickets remain.")
            if not tickets:
                print("Only positive reviews in this window; nothing to report.")
                return

        if args.state:
            state = load_state(args.state)
            new, changed, unchanged = partition_by_state(tickets, state)
            print(f"{len(new)} new, {len(changed)} changed since last reported, "
                  f"{len(unchanged)} unchanged (skipped).")
            stats["skipped_unchanged"] = len(unchanged)
            stats["updated_count"] = len(changed)
            updated_ids = {t.get("id") for t in changed}
            # Skipping before the model call means unchanged tickets cost nothing.
            tickets = new + changed
            if not tickets:
                print("Nothing new or changed since the last run; nothing to report.")
                return

        # Only after the review and dedup filters, so we never pay comment lookups
        # for tickets we are about to discard.
        if not args.no_hydrate:
            hydrate_descriptions(zd, subdomain, tickets)

        analyzed = tickets
        compact = [compact_ticket(t) for t in tickets]

        if args.dump_batch:
            dump_batch(args.dump_batch, compact, model)
            print(f"Wrote {len(compact)} tickets to {args.dump_batch} — this file contains "
                  f"ticket content, so keep it out of the repo.")
            print("Classify it, then: --findings <path> --dry-run")
            return

        client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
        analyzer = partial(analyze, client, resolve_api_model(model), args.effort)
        findings = analyze_in_chunks(analyzer, compact, args.batch_size)

        # Keep only findings whose id maps to a fetched ticket, in case of drift.
        valid_ids = {t["id"] for t in compact}
        findings = [f for f in findings if f.get("id") in valid_ids]

        # A ticket with no classification was never triaged, so it must not be
        # recorded as reported — leave it eligible for the next run.
        finding_ids = {f.get("id") for f in findings}
        classified = [t for t in analyzed if t.get("id") in finding_ids]
        if len(classified) < len(analyzed):
            print(f"Note: {len(analyzed) - len(classified)} ticket(s) came back without a "
                  f"classification; they stay eligible for the next run.")

    # Same selection the digest uses, so the console count can't disagree with the
    # digest — worth_looking_into alone would miss urgent categories the model
    # failed to flag.
    shown, omitted = select_highlights(findings)
    print(f"{len(findings)} tickets classified; {len(shown) + len(omitted)} worth looking into"
          + (f" ({len(omitted)} beyond the display cap)." if omitted else "."))

    messages, coverage = build_messages(findings, subdomain, stats, updated_ids)
    if args.dry_run:
        print(json.dumps(messages, indent=2, ensure_ascii=False))
        if args.state:
            would = set().union(*coverage) if coverage else set()
            print(f"(dry run: would record "
                  f"{sum(1 for t in classified if t.get('id') in would)} tickets "
                  f"in {args.state})")
        return

    posted = post_to_discord(requests.Session(), webhook, messages)
    print(f"Posted {posted} of {len(messages)} Discord message(s).")

    # Record only tickets covered by messages Discord actually accepted, so a partial
    # failure neither reposts what landed nor suppresses what didn't.
    if args.state and state is not None:
        delivered = set().union(*coverage[:posted]) if posted else set()
        recorded = [t for t in classified if t.get("id") in delivered]
        kept, pruned = save_state(args.state, state, recorded, args.state_retention_days)
        print(f"Recorded {len(recorded)} tickets; state now tracks {kept} "
              f"({pruned} pruned beyond {args.state_retention_days} days).")

    if posted < len(messages):
        sys.exit(f"Aborted after {posted}/{len(messages)} messages; "
                 f"undelivered tickets stay eligible for the next run.")


if __name__ == "__main__":
    main()
