"""
Draft a reply, unprompted, on the digest's tickets that support already knows how to answer.

"Knows" means a human wrote that answer before: a `claude: draft` brief, for the same
problem group in the house answers, that went out as the ticket's first reply with no
amendment. Those briefs are cached in a file beside the digest's state and topped up
from `tags:claude-sent` on every run.

For each new ticket nobody has answered, Claude places it in a group; only a group in
DRAFT_GROUPS is drafted. Claude picks the past brief that fully answers it (or none),
and composes from that brief exactly as `claude: draft` would. A group in
STANDARD_REPLIES gets its fixed text instead, uncomposed, and a category in
STANDING_BRIEFS is composed from its fixed brief.
Each is the same private draft note, so an agent sends it with `claude: reply N`, or
replaces it with `claude: draft <a new brief>`.

Nothing here writes a public comment. Like the rest of the Zendesk jobs, nothing is
printed but ticket ids and counts: this repo and its logs are public.
"""
import json
import re
import sys
import textwrap
import time
from datetime import date, timedelta

from session_ops.shared.discord import clip
from session_ops.shared.state import write_json
from session_ops.zendesk import api, claude_api, note_reply

CACHE_VERSION = 1
# Older briefs drop out: a fact support stated half a year ago is likelier to be stale.
BRIEF_MAX_AGE_DAYS = 180
# Zendesk's date search is day-granular in the account's timezone, and `updated>` is
# exclusive, so each run re-reads two days to be sure nothing slips between runs.
RESCAN_DAYS = 2
MAX_CANDIDATES = 6
SELECT_TIMEOUT_SECONDS = 120
# Bounds the digest's runtime: a ticket costs up to three Claude calls.
BUDGET_SECONDS = 20 * 60
# A dead key or an exhausted balance fails every ticket the same way.
MAX_FAILURES = 3
# A runaway guard on the backfill, far above the sent tickets one retention window holds.
MAX_SENT_TICKETS = 20000

SKIP_CATEGORIES = frozenset({"spam_or_solicitation", "positive_review"})

# The house-answer groups whose first reply support settled on; the rest vary too much
# per ticket for an unprompted draft to be worth a reviewer's time.
DRAFT_GROUPS = frozenset({
    "reporting-illegal-content",
    "needs-more-detail",
    "account-unrecoverable-without-recovery-phrase",
    "recovery-phrase-location-and-login-basics",
    "deleted-data-not-recoverable",
    "lost-contacts-not-restorable",
    "law-enforcement-requests",
    "push-notifications-not-arriving",
    "messages-failing-to-send",
    "connection-path-errors",
    "attachments-failing-to-send",
    "call-failures-and-quality",
    "messages-notified-but-not-appearing",
    "delete-account-and-data",
    "leaked-recovery-phrase",
})

# House-answer groups answered with one fixed text, never composed or translated: what
# Session says to an authority has to be the same every time. Sent only by a human's
# `claude: reply`, like every other draft.
STANDARD_REPLIES = {
    "law-enforcement-requests": ("law-enforcement requests", """\
We’ve received your request.

Here’s what we can share about Session and legal requests:
Session is a decentralized, privacy-focused messenger, and we are not the sole operators of the network.
The Session Technology Foundation does not collect or store personal data such as phone numbers, email addresses, or identifying metadata like IP addresses.
Because of how Session is built, we don’t have the technical ability to preserve or provide information about user identities, connections, or data. That includes IP logs or timestamps for messages, calls, video calls, or media.

What we can do:
If you provide the relevant Session ID, we can block that ID from accessing Session-operated communities, which are mainly used for Session updates and information. If you’d like us to take that step, share the ID and we’ll proceed.

Legal process:
As a Swiss organization, legal inquiries must be processed via the Swiss Post and Telecommunications Surveillance Service (PTSS) under Art. 26 para. 2 VÜPF.
For foreign authorities, requests need to come through mutual legal assistance channels under the Swiss Federal Act on International Mutual Assistance in Criminal Matters. We cannot act on foreign production or preservation orders sent directly to us.

If you need anything clarified or want us to action a community block with a Session ID, please reply here."""),
}


# Triage categories answered from a standing brief, composed in the reporter's language.
STANDING_BRIEFS = {
    "abuse_report": ("the usual reply to abuse reports", """\
Thank them for the report and say we take it seriously.
Session is decentralized: nobody, Session included, can ban, remove or deactivate a Session ID across the network, or see what it sends.
The one thing we can do is block the reported Session ID from the communities Session itself operates. If they gave the full Session ID, say we can do that with it; if not, ask for it.
If the account or the content also appears on another website or app, they should report it to that platform too.
They should not send us the illegal content itself."""),
}


def standard_result(text):
    """A fixed reply in the shape compose returns, as its only option."""
    return {"language": "English", "language_code": "en", "is_english": True,
            "options": [{"approach": "the standard reply", "reply_en": text,
                         "translated": text, "back_translation": ""}]}


# ---- What support already answered -------------------------------------------


def sent_first_brief(comments, api_user):
    """(brief, sent_at) when the ticket's first reply went out from one unamended
    `claude: draft`, else None. `comments` oldest first.

    Amended or redrafted briefs are left out: each is a change to a draft, not an answer.
    """
    draft = None
    for comment in comments:
        author = comment.get("author_id")
        if comment.get("public"):
            if author != api_user:
                continue
            if draft and draft["replied"] and draft["brief"]:
                return draft["brief"], comment.get("created_at")
            return None
        if author == api_user:
            continue
        parsed = note_reply.parse_command(note_reply.comment_text(comment))
        if not parsed:
            continue
        action, brief = parsed
        if action == "draft":
            if draft:
                return None
            draft = {"brief": brief, "replied": False}
        elif action == "reply" and draft:
            draft["replied"] = True
    return None


def load_cache(path):
    """The brief cache, or an empty one when it is missing or unreadable: rebuilding it
    costs one backfill of `tags:claude-sent`, never a wrong draft."""
    try:
        with open(path, encoding="utf-8") as handle:
            cache = json.load(handle)
        if cache.get("version") == CACHE_VERSION and isinstance(cache.get("briefs"), dict):
            return cache
    except (OSError, ValueError, AttributeError):
        pass
    return {"version": CACHE_VERSION, "scanned_through": None, "briefs": {}}


def refresh_cache(session, subdomain, api_user, cache, today):
    """Add the briefs sent since the last scan, and age out the old ones.

    The scan only moves on when it read everything: a truncated search or a failed
    comment read is retried from the same point next run.
    """
    cutoff = (today - timedelta(days=BRIEF_MAX_AGE_DAYS)).isoformat()
    since = cache.get("scanned_through") or cutoff
    query = (f"type:ticket tags:claude-sent updated>{since} "
             "order_by:created_at sort:desc")
    tickets, matched = api.fetch_every_ticket(session, subdomain, query, MAX_SENT_TICKETS)
    complete = matched is None or len(tickets) >= matched
    added = 0
    for ticket in tickets:
        group, platform = note_reply.tagged_placement(ticket)
        if not group:
            continue
        comments = api.fetch_comments(session, subdomain, ticket["id"], order="asc",
                                      optional=True)
        if comments is None:
            complete = False
            continue
        found = sent_first_brief(comments, api_user)
        if found:
            cache["briefs"][str(ticket["id"])] = {
                "group": group, "platform": platform or "unknown",
                "brief": found[0], "sent_at": found[1]}
            added += 1
    cache["briefs"] = {tid: b for tid, b in cache["briefs"].items()
                       if (b.get("sent_at") or "") >= cutoff}
    if complete:
        cache["scanned_through"] = (today - timedelta(days=RESCAN_DAYS)).isoformat()
    print(f"Known briefs: read {len(tickets)} of {matched} sent ticket(s), {added} "
          f"first-reply brief(s); {len(cache['briefs'])} cached"
          + ("." if complete else "; incomplete, rescanned next run."))
    return cache


def candidates(cache, group):
    """The group's distinct briefs, most used then most recent first.

    Each is {brief, uses, latest, platforms, source}, `source` being the newest ticket
    it was sent on.
    """
    merged = {}
    for tid, entry in cache["briefs"].items():
        if entry.get("group") != group:
            continue
        key = re.sub(r"\s+", " ", entry["brief"]).strip().lower()
        row = merged.setdefault(key, {"brief": entry["brief"], "uses": 0, "latest": "",
                                      "platforms": set(), "source": tid})
        row["uses"] += 1
        row["platforms"].add(entry.get("platform") or "unknown")
        if (entry.get("sent_at") or "") > row["latest"]:
            row.update(latest=entry["sent_at"], source=tid, brief=entry["brief"])
    rows = sorted(merged.values(), key=lambda r: (r["uses"], r["latest"]), reverse=True)
    return rows[:MAX_CANDIDATES]


# ---- Choosing a brief -------------------------------------------------------

SELECT_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["choice", "reason"],
    "properties": {
        "choice": {"type": "integer",
                   "description": "The number of the brief that answers this ticket, "
                                  "or 0 when none does."},
        "reason": {"type": "string", "description": "One short sentence, in English."},
    },
}

SELECT_SYSTEM = textwrap.dedent(
    """
    You decide whether an answer Session support already gave other customers also
    answers this one, unchanged. Session is a private messenger.

    You are given one new ticket and numbered BRIEFS: the substance of a first reply
    support wrote for the same kind of problem, each one reviewed by a human and sent.
    Pick the brief that, on its own, answers what this customer actually asks. Answer
    0 when none does.

    Pick a brief only when all of these hold:
    - It answers this customer's actual question or problem, not just its topic.
    - It needs nothing it does not say: it names no other ticket, person or
      conversation, and does not rely on details only that other customer gave.
    - Every fact in it is still true TODAY. A brief that mentions a pending or
      upcoming release, a fix being prepared, "this week", "soon" or an ETA was true
      when it was sent; reject it unless it was sent within the last 14 days.
    - It does not say an action was or will be taken on that customer's case: an
      account banned, a bug filed, a refund, an escalation. Nobody has acted on this
      ticket.

    Asking for logs or missing details is a valid answer when the ticket lacks what
    support needs to help. A brief is an agent's shorthand: judge what it says, not
    how it reads. Prefer one sent several times.

    When in doubt, answer 0. A missing draft costs an agent one note; a wrong one
    costs a reviewer's time and may be sent.
    """
).strip()


def build_select_prompt(ticket, sample, rows, today):
    parts = [f"TODAY: {today.isoformat()}", "",
             f"THE TICKET: {(ticket.get('subject') or '')[:200]}", sample, "", "BRIEFS:"]
    for number, row in enumerate(rows, 1):
        parts += [f"{number}. sent {row['uses']} time(s), most recently "
                  f"{(row['latest'] or '')[:10]}, platforms: "
                  f"{', '.join(sorted(row['platforms']))}",
                  row["brief"], ""]
    return "\n".join(parts)


def choose_brief(ticket, sample, rows, today):
    """The chosen row, or None."""
    found = claude_api.claude_json(
        "medium", SELECT_SYSTEM, SELECT_SCHEMA,
        build_select_prompt(ticket, sample, rows, today),
        SELECT_TIMEOUT_SECONDS, f"the brief choice for #{ticket['id']}")
    choice = found.get("choice")
    if isinstance(choice, int) and 1 <= choice <= len(rows):
        return rows[choice - 1]
    return None


# ---- Drafting ---------------------------------------------------------------


def eligible(ticket, comments, authors, api_user):
    """Whether nobody has answered or picked up this ticket yet.

    Only a first reply: a brief that answered someone's opening message does not answer
    their follow-up. Any private note from a person means someone is on it.
    """
    tags = set(ticket.get("tags") or [])
    if tags & {note_reply.TAG_DRAFTED, note_reply.TAG_QUEUED, note_reply.TAG_SENT}:
        return False
    for comment in comments:
        author = comment.get("author_id")
        if comment.get("public"):
            if author not in authors:
                return False
        elif author != api_user and author not in authors:
            return False
    return True


def write_draft(session, subdomain, ticket_id, result, brief, unprompted, dry_run,
                cell=None, title=None, covering=None, tags=()):
    if dry_run:
        return
    note_reply.write_to_ticket(
        session, subdomain, ticket_id,
        note_reply.build_draft_note(result, brief, None, cell=cell, title=title,
                                    covering=covering, unprompted=unprompted),
        public=False, as_html=True, add_tags=[note_reply.TAG_DRAFTED, *tags])


def draft_ticket(session, subdomain, ticket, cache, book, api_user, today, dry_run,
                 category=None):
    """Draft one ticket. Returns the source ticket id whose brief was reused, the group
    or category of a standard reply or standing brief, or None.

    `category` is the digest's. Placement runs first even for a category with a
    standing brief: a police request about abuse has to get the fixed legal text.
    """
    ticket_id = ticket["id"]
    comments = api.fetch_comments(session, subdomain, ticket_id)
    public = [c for c in comments if c.get("public")]
    authors = api.customer_authors(session, subdomain, ticket, public)
    if not eligible(ticket, comments, authors, api_user):
        return None
    sample = api.customer_text(session, subdomain, ticket, comments,
                               note_reply.CUSTOMER_SAMPLE_CHARS, authors)
    group, cell, covering, title, precedent, new_tags = note_reply.ground(
        ticket, sample, book)
    if group not in DRAFT_GROUPS:
        if new_tags and not dry_run:
            note_reply.change_tags(session, subdomain, ticket_id, add=new_tags)
        return None
    if group in STANDARD_REPLIES:
        name, text = STANDARD_REPLIES[group]
        print(f"#{ticket_id}: {group}, drafted the standard reply.")
        write_draft(session, subdomain, ticket_id, standard_result(text), None,
                    (f"the standard reply to {name}, word for word", None, group),
                    dry_run, tags=new_tags)
        return group
    if category in STANDING_BRIEFS:
        name, brief = STANDING_BRIEFS[category]
        result = note_reply.compose(clip(sample, note_reply.CUSTOMER_SAMPLE_CHARS), brief)
        print(f"#{ticket_id}: {category}, drafted {len(result['options'])} option(s) "
              f"from the standing brief.")
        write_draft(session, subdomain, ticket_id, result, brief,
                    (name, "Standing brief:", category), dry_run, tags=new_tags)
        return category
    rows = candidates(cache, group)
    if not rows:
        if new_tags and not dry_run:
            note_reply.change_tags(session, subdomain, ticket_id, add=new_tags)
        return None
    row = choose_brief(ticket, sample, rows, today)
    if not row:
        print(f"#{ticket_id}: {group}, no known brief answers it.")
        if new_tags and not dry_run:
            note_reply.change_tags(session, subdomain, ticket_id, add=new_tags)
        return None
    result = note_reply.compose(clip(sample, note_reply.CUSTOMER_SAMPLE_CHARS),
                                row["brief"], precedent=precedent)
    print(f"#{ticket_id}: {group}, drafted {len(result['options'])} option(s) from the "
          f"brief sent on #{row['source']}.")
    write_draft(session, subdomain, ticket_id, result, row["brief"],
                (f"a reply unprompted, reusing the brief support sent on #{row['source']}",
                 f"Brief reused from #{row['source']}:", row["source"]),
                dry_run, cell, title, covering, new_tags)
    return row["source"]


def draft_known(session, subdomain, tickets, findings, cache_path, skip_ids=(),
                dry_run=False):
    """Draft every ticket in `tickets` a known brief answers. Returns the drafted ids.

    `findings` is the digest's classification, read for the category; `skip_ids` are
    tickets whose newest message is not on the ticket yet. A dry run writes neither
    notes nor the cache. Every failure here is reported and survived: the digest is
    the job, and these drafts are a head start on it.
    """
    book = note_reply.load_house()
    if not book:
        print(f"Note: no house answers ({note_reply.HOUSE_ENV}), so no ticket can be "
              f"matched to a known brief; drafting skipped.")
        return []
    today = date.today()
    started = time.monotonic()
    try:
        api_user = note_reply.api_user_id(session, subdomain)
        cache = refresh_cache(session, subdomain, api_user, load_cache(cache_path), today)
    # Broad on purpose, here and per ticket: drafting runs before the digest posts, so
    # anything it raises would cost the digest itself.
    except (Exception, SystemExit) as exc:
        print(f"Note: could not read the known briefs ({type(exc).__name__}: {exc}); "
              f"drafting skipped.")
        return []
    if not dry_run:
        try:
            write_json(cache_path, cache)
        except OSError as exc:
            print(f"Note: could not save the known briefs ({exc}).", file=sys.stderr)

    category = {f.get("id"): f.get("category") for f in findings}
    rank = {f.get("id"): f.get("priority_rank", 9999) for f in findings}
    queue = sorted((t for t in tickets
                    if t["id"] in category and t["id"] not in skip_ids
                    and category[t["id"]] not in SKIP_CATEGORIES),
                   key=lambda t: rank[t["id"]])
    drafted, failures = [], 0
    for position, ticket in enumerate(queue):
        if time.monotonic() - started > BUDGET_SECONDS:
            print(f"Drafting stopped at its time budget; {len(queue) - position} "
                  f"ticket(s) not considered.")
            break
        try:
            if draft_ticket(session, subdomain, ticket, cache, book, api_user, today,
                            dry_run, category[ticket["id"]]):
                drafted.append(ticket["id"])
        except (Exception, SystemExit) as exc:
            failures += 1
            print(f"#{ticket['id']}: not drafted ({type(exc).__name__}: {exc}).",
                  file=sys.stderr)
            if failures >= MAX_FAILURES:
                print(f"Drafting stopped after {failures} failures.")
                break
    print(f"Drafted {len(drafted)} of {len(queue)} ticket(s) from known briefs"
          + (" (dry run, nothing written)." if dry_run else "."))
    return drafted
