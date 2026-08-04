#!/usr/bin/env python3
"""
Daily Zendesk ticket triage with Claude, delivered to Discord.

Fetches open Zendesk tickets (broad query by default, not just bugs), sends the
whole batch to Claude in a single structured-output request, and posts a Discord
summary that links back to each original ticket and highlights the ones worth
looking into (crashes, data loss, legal requests, security/legislation, etc.).

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

Config (env vars, or CLI flags for local runs):
    ZENDESK_SUBDOMAIN     e.g. "mycompany"  -> https://mycompany.zendesk.com
    ZENDESK_EMAIL         agent email for API token auth
    ZENDESK_API_TOKEN     Zendesk API token
    ANTHROPIC_API_KEY     Claude API key (read by the SDK automatically)
    DISCORD_WEBHOOK_URL   Discord incoming webhook
    ZENDESK_QUERY         (optional) Zendesk search query; see DEFAULT_QUERY
    ZENDESK_TRIAGE_MODEL  (optional) Claude model id; defaults to claude-opus-4-8

Usage:
    # real run (CI): reads everything from the environment
    python triage.py

    # local dry run: fetch + analyze, print the Discord payload, post nothing
    python triage.py --dry-run

    # what the scheduled daily run does: 48h window, skipping unchanged repeats
    python triage.py --window-hours 48 --state .triage-state/seen.json

    # or an explicit query, which overrides --window-hours
    python triage.py --query "type:ticket status:open tags:bug" --max-tickets 50

    # local debugging without an ANTHROPIC_API_KEY: classify via the `claude` CLI
    python triage.py --backend claude-cli --dry-run --max-tickets 20

    # or split it in two: dump the batch, classify it by hand, feed it back
    python triage.py --dump-batch /tmp/batch.json --max-tickets 20
    python triage.py --backend file --findings /tmp/findings.json --dry-run
"""
import argparse
import json
import os
import subprocess
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
DEFAULT_MODEL = "claude-opus-4-8"
DEFAULT_MAX_TICKETS = 100
DESCRIPTION_CHARS = 1500  # per-ticket description sent to Claude (triage only)
# One classification runs ~100 output tokens per ticket, and adaptive thinking draws
# from the same max_tokens budget. 400 keeps a chunk far under the 128K output
# ceiling; batches larger than this are split rather than truncated.
DEFAULT_BATCH_SIZE = 400
MAX_OUTPUT_TOKENS = 128000

CATEGORIES = [
    "bug_report",
    "low_star_review",
    "legal_request",
    "security_or_legislation",
    "question",
    "feature_request",
    "other",
]
SEVERITIES = ["crash", "data_loss", "major", "minor", "cosmetic", "not_applicable"]

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
        "description": "True if a human should review this soon (major bug, legal, security/legislation, or otherwise notable).",
    },
    "cluster": {
        "type": "string",
        "description": "Short label grouping tickets with the same likely root cause; identical labels mean likely duplicates/clusters. Empty string if standalone.",
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

SYSTEM_PROMPT = textwrap.dedent(
    """
    You are a senior support-triage engineer. You are given a batch of Zendesk
    support tickets as JSON. For every ticket, return a classification object.

    Guidelines:
    - Categorise each ticket: bug_report, low_star_review (app-store style
      complaints, which often hide real bugs — extract the bug in `summary`),
      legal_request, security_or_legislation (e.g. law/regulation questions such
      as "Chat Control", data requests, encryption/backdoor questions),
      question, feature_request, or other.
    - Infer severity from the description: crash and data_loss are the most
      serious; use not_applicable for tickets that are not bug reports.
    - Group tickets that share a likely root cause under the same short `cluster`
      label, even if worded differently or in different languages. Identical
      labels flag likely duplicates/clusters.
    - `summary` is one plain line: what is actually broken, or what the user
      actually wants. Do not restate the ticket subject.
    - Mark `worth_looking_into` true for anything a human should see soon:
      crashes, data loss, major bugs, legal requests, and
      security/legislation topics. Be selective — not everything is major.
    - Rank by `priority_rank` (1 = first) across the whole batch.
    - Echo each ticket `id` back exactly. Return one object per input ticket.
    """
).strip()


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


def request_with_retry(session, method, url, attempts=6, **kwargs):
    """GET/POST with backoff on 429 and 5xx.

    Lower `attempts` for calls whose result is nice-to-have: the full budget can
    burn ~60s of backoff, which is not worth spending on optional data.
    """
    delay = 1.0
    last_exc = None
    for _ in range(attempts):
        try:
            resp = session.request(method, url, timeout=30, **kwargs)
        except requests.RequestException as exc:
            last_exc = exc
            time.sleep(delay)
            delay = min(delay * 2, 30)
            continue
        if resp.status_code == 429 or resp.status_code >= 500:
            retry_after = float(resp.headers.get("retry-after", delay))
            time.sleep(min(retry_after, 60))
            delay = min(delay * 2, 30)
            continue
        return resp
    if last_exc:
        raise last_exc
    return resp


def fetch_tickets(session, subdomain, query, max_tickets):
    """Fetch tickets via the Zendesk Search API, following pagination.

    Returns (tickets, total_matched). total_matched is the full result count
    reported by Zendesk, which can exceed len(tickets) when max_tickets caps the
    batch — the caller surfaces that gap so the truncation isn't silent.
    """
    base = f"https://{subdomain}.zendesk.com/api/v2/search.json"
    url = base
    params = {"query": query, "per_page": 100}
    tickets = []
    total_matched = None
    while url and len(tickets) < max_tickets:
        resp = request_with_retry(session, "GET", url, params=params)
        params = None  # next_page already carries the query
        if resp.status_code == 403:
            sys.exit("Zendesk returned 403 — the API token/email may lack search access.")
        if resp.status_code >= 400:
            sys.exit(f"Zendesk search failed ({resp.status_code}): {resp.text[:300]}")
        payload = resp.json()
        if total_matched is None:
            total_matched = payload.get("count")
        for row in payload.get("results", []):
            if row.get("result_type") != "ticket":
                continue
            tickets.append(row)
            if len(tickets) >= max_tickets:
                break
        url = payload.get("next_page")
    return tickets, total_matched


def fetch_total_unsolved(session, subdomain):
    """Count the whole unsolved backlog. Best effort: returns None on failure.

    Context for the digest, not something to hold the run up for — hence the
    short retry budget.
    """
    url = f"https://{subdomain}.zendesk.com/api/v2/search/count.json"
    resp = request_with_retry(
        session, "GET", url, attempts=2, params={"query": BACKLOG_QUERY}
    )
    if resp.status_code >= 400:
        print(f"Note: could not count the unsolved backlog ({resp.status_code}).")
        return None
    return resp.json().get("count")


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


def build_analysis_prompt(compact_tickets):
    return (
        "Classify every ticket in this batch and return one object per ticket.\n\n"
        "TICKETS (JSON):\n"
        + json.dumps(compact_tickets, ensure_ascii=False)
    )


# The API path gets the shape enforced by structured outputs. The CLI path has no
# such enforcement, so the shape has to be spelled out in the prompt instead.
CLI_JSON_INSTRUCTIONS = textwrap.dedent(
    """
    Return ONLY a single JSON object — no prose, no explanation, no markdown code
    fence. The object has exactly one key, "tickets", whose value is an array with
    one object per input ticket, each with exactly these keys:
      id (integer, echoed back unchanged), category, severity, affected_component,
      summary, likely_root_cause, language, priority_rank (integer),
      worth_looking_into (boolean), cluster
    category must be one of: {categories}
    severity must be one of: {severities}
    Use an empty string for text fields you cannot fill.
    """
).strip().format(categories=", ".join(CATEGORIES), severities=", ".join(SEVERITIES))


def extract_json_object(text):
    """Pull the outermost JSON object out of model prose (tolerates code fences)."""
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        sys.exit(f"No JSON object found in the model output:\n{text[:500]}")
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        sys.exit(f"Model output was not valid JSON ({exc}):\n{text[start : start + 500]}")


def analyze_via_claude_cli(model, compact_tickets, timeout=1800):
    """Classify the batch with the local `claude` CLI instead of the Anthropic API.

    Local debugging path: it authenticates as Claude Code, so no ANTHROPIC_API_KEY is
    needed. There is no structured-output enforcement here, so the response is parsed
    defensively and the schema is described in the prompt.
    """
    prompt = "\n\n".join(
        [SYSTEM_PROMPT, CLI_JSON_INSTRUCTIONS, build_analysis_prompt(compact_tickets)]
    )
    cmd = ["claude", "-p", "--output-format", "json"]
    if model:
        cmd += ["--model", model]
    try:
        # Prompt goes over stdin: a full batch can exceed the argv size limit.
        proc = subprocess.run(
            cmd, input=prompt, capture_output=True, text=True, timeout=timeout
        )
    except FileNotFoundError:
        sys.exit("`claude` not found on PATH. Install Claude Code, or use --backend api.")
    except subprocess.TimeoutExpired:
        sys.exit(f"`claude` timed out after {timeout}s. Try a smaller --max-tickets.")
    if proc.returncode != 0:
        sys.exit(f"`claude` failed ({proc.returncode}): {proc.stderr[:500]}")

    envelope = extract_json_object(proc.stdout)
    if envelope.get("is_error") or envelope.get("subtype") != "success":
        sys.exit(f"`claude` reported an error: {envelope.get('result') or envelope}")
    cost = envelope.get("total_cost_usd")
    if cost is not None:
        print(f"claude CLI reported ${cost:.4f} for this batch.")
    return extract_json_object(envelope.get("result") or "")["tickets"]


def dump_batch(path, compact_tickets, model):
    """Write the batch to disk so it can be classified by hand. Contains ticket text."""
    payload = {
        "model": model,
        "system_prompt": SYSTEM_PROMPT,
        "instructions": CLI_JSON_INSTRUCTIONS,
        "schema": SCHEMA,
        "tickets": compact_tickets,
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)


def load_findings(path):
    """Read findings written by hand or by another tool: {"tickets": [...]} or [...]."""
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    findings = data.get("tickets") if isinstance(data, dict) else data
    if not isinstance(findings, list):
        sys.exit(f"{path}: expected a JSON list, or an object with a 'tickets' list.")
    return findings


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
    return json.loads(text)["tickets"]


# ---- Discord rendering -----------------------------------------------------

SEVERITY_COLOR = {
    "crash": 0xE74C3C,      # red
    "data_loss": 0xC0392B,  # dark red
    "major": 0xE67E22,      # orange
    "minor": 0xF1C40F,      # yellow
    "cosmetic": 0x95A5A6,   # grey
    "not_applicable": 0x3498DB,  # blue
}
CATEGORY_LABEL = {
    "bug_report": "🐞 Bug report",
    "low_star_review": "⭐ Low-star review",
    "legal_request": "⚖️ Legal request",
    "security_or_legislation": "🔒 Security / legislation",
    "question": "❓ Question",
    "feature_request": "💡 Feature request",
    "other": "• Other",
}
MAX_EMBEDS_PER_MESSAGE = 10
MAX_HIGHLIGHTS = 27  # 3 messages of ~9 highlights + a summary embed


def ticket_url(subdomain, ticket_id):
    return f"https://{subdomain}.zendesk.com/agent/tickets/{ticket_id}"


def clip(text, limit):
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def build_summary_embed(findings, highlights, subdomain, stats=None):
    by_category = {}
    by_severity = {}
    clusters = {}
    for f in findings:
        by_category[f["category"]] = by_category.get(f["category"], 0) + 1
        by_severity[f["severity"]] = by_severity.get(f["severity"], 0) + 1
        label = (f.get("cluster") or "").strip()
        if label:
            clusters.setdefault(label, []).append(f["id"])

    cat_lines = "\n".join(
        f"{CATEGORY_LABEL.get(cat, cat)}: **{count}**"
        for cat, count in sorted(by_category.items(), key=lambda kv: -kv[1])
    )
    serious = by_severity.get("crash", 0) + by_severity.get("data_loss", 0)
    dup_clusters = {k: v for k, v in clusters.items() if len(v) > 1}
    fields = [{"name": "By category", "value": cat_lines or "—", "inline": False}]
    if dup_clusters:
        cluster_lines = "\n".join(
            f"**{clip(label, 40)}** — {len(ids)} tickets (#{', #'.join(str(i) for i in ids[:6])})"
            for label, ids in sorted(dup_clusters.items(), key=lambda kv: -len(kv[1]))[:6]
        )
        fields.append({"name": "Likely duplicate clusters", "value": clip(cluster_lines, 1024), "inline": False})

    # Account for the batch honestly: how many of the window we looked at, how many
    # we skipped as unchanged, and how big the untriaged backlog is behind it.
    stats = stats or {}
    matched = stats.get("matched")
    skipped = stats.get("skipped_unchanged") or 0
    updated = stats.get("updated_count") or 0
    backlog = stats.get("total_unsolved")

    window = f"Analyzed **{len(findings)}**"
    if matched is not None:
        window += f" of **{matched}**"
    window += f" tickets in the window"
    if stats.get("scope"):
        window += f" ({stats['scope']})"
    window += "."
    if skipped:
        window += f" Skipped **{skipped}** already reported and unchanged."
    lines = [window]

    if backlog is not None:
        lines.append(f"Backlog: **{backlog:,}** unsolved tickets in total (not triaged).")

    tail = f"**{len(highlights)}** worth looking into"
    tail += f", including **{serious}** crash/data-loss." if serious else "."
    if updated:
        tail += f" 🔄 **{updated}** changed since last reported."
    lines.append(tail)

    return {
        "title": "🗂️ Zendesk triage",
        "description": "\n".join(lines),
        "color": 0xE67E22 if highlights else 0x2ECC71,
        "fields": fields,
    }


def build_highlight_embed(finding, subdomain, is_update=False):
    tid = finding["id"]
    sev = finding.get("severity", "not_applicable")
    cat = CATEGORY_LABEL.get(finding.get("category"), finding.get("category", ""))
    # 🔄 marks a ticket we already showed that has since changed, so the reader
    # knows it is a follow-up rather than a duplicate post.
    marker = "🔄 " if is_update else ""
    title = f"{marker}#{tid} · {clip(finding.get('summary'), 200) or '(no summary)'}"
    fields = [
        {"name": "Category", "value": clip(cat, 60) or "—", "inline": True},
        {"name": "Severity", "value": sev, "inline": True},
        {"name": "Language", "value": clip(finding.get("language"), 40) or "—", "inline": True},
    ]
    component = clip(finding.get("affected_component"), 100)
    if component:
        fields.append({"name": "Component", "value": component, "inline": True})
    root = clip(finding.get("likely_root_cause"), 300)
    description = f"Likely cause: {root}" if root else ""
    return {
        "title": clip(title, 256),
        "url": ticket_url(subdomain, tid),
        "description": description,
        "color": SEVERITY_COLOR.get(sev, 0x95A5A6),
        "fields": fields,
    }


def build_messages(findings, subdomain, stats=None, updated_ids=None):
    """Return a list of Discord webhook payloads (each <=10 embeds)."""
    updated_ids = updated_ids or set()
    highlights = [f for f in findings if f.get("worth_looking_into")]
    highlights.sort(key=lambda f: f.get("priority_rank", 9999))
    shown = highlights[:MAX_HIGHLIGHTS]

    embeds = [build_summary_embed(findings, highlights, subdomain, stats)]
    embeds += [
        build_highlight_embed(f, subdomain, is_update=f.get("id") in updated_ids)
        for f in shown
    ]

    content = None
    if len(highlights) > len(shown):
        content = f"Showing the top {len(shown)} of {len(highlights)} tickets worth looking into."

    messages = []
    for i in range(0, len(embeds), MAX_EMBEDS_PER_MESSAGE):
        chunk = embeds[i : i + MAX_EMBEDS_PER_MESSAGE]
        payload = {"embeds": chunk}
        if i == 0 and content:
            payload["content"] = content
        messages.append(payload)
    return messages


def post_to_discord(session, webhook_url, messages):
    for payload in messages:
        resp = request_with_retry(session, "POST", webhook_url, json=payload)
        if resp.status_code >= 400:
            sys.exit(f"Discord webhook failed ({resp.status_code}): {resp.text[:300]}")


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
    parser.add_argument("--model", help="Claude model id (else ZENDESK_TRIAGE_MODEL, else claude-opus-4-8).")
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
    parser.add_argument("--backend", default="api", choices=["api", "claude-cli", "file"],
                        help="Where classification happens: the Anthropic API (default), the "
                             "local `claude` CLI (no API key needed), or a findings file.")
    parser.add_argument("--findings",
                        help="Findings JSON to render instead of classifying (--backend file).")
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

    if args.backend == "file" and not args.findings:
        sys.exit("--backend file requires --findings PATH.")

    # Subdomain is always needed: it builds the ticket links in the Discord payload.
    subdomain = get_env("ZENDESK_SUBDOMAIN", args.subdomain)
    # A dump exits before rendering anything, so it never needs the webhook either.
    needs_webhook = not (args.dry_run or args.dump_batch)
    webhook = get_env("DISCORD_WEBHOOK_URL", args.webhook, required=needs_webhook)
    model = args.model or os.environ.get("ZENDESK_TRIAGE_MODEL") or DEFAULT_MODEL

    stats = {}
    state = None
    reported = []
    updated_ids = set()

    if args.backend == "file":
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
            print(f"Note: {total_matched - len(tickets)} matching tickets were not analyzed "
                  f"(--max-tickets is {args.max_tickets}).")
        if not tickets:
            print("No tickets matched the query; nothing to do.")
            return

        stats["matched"] = total_matched
        stats["total_unsolved"] = fetch_total_unsolved(zd, subdomain)

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

        reported = tickets
        compact = [compact_ticket(t) for t in tickets]

        if args.dump_batch:
            dump_batch(args.dump_batch, compact, model)
            print(f"Wrote {len(compact)} tickets to {args.dump_batch} — this file contains "
                  f"ticket content, so keep it out of the repo.")
            print("Classify it, then: --backend file --findings <path> --dry-run")
            return

        if args.backend == "claude-cli":
            analyzer = partial(analyze_via_claude_cli, model)
        else:
            client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
            analyzer = partial(analyze, client, model, args.effort)
        findings = analyze_in_chunks(analyzer, compact, args.batch_size)

        # Keep only findings whose id maps to a fetched ticket, in case of drift.
        valid_ids = {t["id"] for t in compact}
        findings = [f for f in findings if f.get("id") in valid_ids]

    highlights = sum(1 for f in findings if f.get("worth_looking_into"))
    print(f"{len(findings)} tickets classified; {highlights} worth looking into.")

    messages = build_messages(findings, subdomain, stats, updated_ids)
    if args.dry_run:
        print(json.dumps(messages, indent=2, ensure_ascii=False))
        if args.state:
            print(f"(dry run: would record {len(reported)} tickets in {args.state})")
        return

    post_to_discord(requests.Session(), webhook, messages)
    print(f"Posted {len(messages)} Discord message(s).")

    # Only after Discord accepted the post: a failure here must leave the tickets
    # unrecorded so the next run retries them instead of dropping them silently.
    if args.state and state is not None:
        kept, pruned = save_state(args.state, state, reported, args.state_retention_days)
        print(f"Recorded {len(reported)} tickets; state now tracks {kept} "
              f"({pruned} pruned beyond {args.state_retention_days} days).")


if __name__ == "__main__":
    main()
