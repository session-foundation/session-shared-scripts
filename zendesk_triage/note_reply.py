#!/usr/bin/env python3
"""
Draft and send Zendesk replies from private notes on the ticket itself.

An agent writes a private note saying what the answer is; Claude writes it properly,
in the language the requester uses, and posts it back as a private note. The agent
reads it and writes a second note to send it.

    claude: draft - attachments are only kept on the server for 14 days. A second
            device that was offline for longer cannot fetch them.

    claude: reply

`claude: english` is the read-only one: it puts the conversation in English on the
ticket so an agent can read a ticket in a language they do not speak.

    ⚠️  `reply` writes a public comment, which emails the requester. That is not
        reversible. --dry-run does everything except the writes.

Unlike reply.py — where the agent types the exact English text and Claude only
translates it — here Claude *composes* the reply from a brief. So every draft is
reviewed before it goes out, English ones included: nobody has read that wording yet.

Two actions, both read from the ticket's own comments:

  draft    Compose the reply from the brief, translate it into the requester's
           language, and post it as a private note with a back-translation.
  reply    Send the most recent draft, verbatim. It never re-composes: what was
           reviewed is what goes out, or the review means nothing.
  english  Post the whole conversation, both sides, in English as a private note.
           Reads only; the customer never sees it.
  solve    Solve the ticket, writing nothing to the customer. For the ones that
           need no reply at all. The private note records who asked and why.
  explain  Post what support usually replies to this kind of ticket, and what was
           actually done about it before — fixes shipped, bugs filed, escalations.
           Reads only. This is where known fixes are surfaced, because `draft`
           refuses to assert one that is not in the brief.

The action comes from the newest private note that parses as a command, so the
webhook only has to say which ticket changed. Notes written by the API user are
skipped, which is what stops Claude's own drafts from re-triggering it.

Because this repo is public, nothing here prints ticket content, briefs, or reply
text. stdout gets ticket ids and outcomes.

Config (env vars, or flags for local runs):
    ZENDESK_SUBDOMAIN     e.g. "mycompany"  -> https://mycompany.zendesk.com
    ZENDESK_EMAIL         agent email for API token auth; authors every comment
    ZENDESK_API_TOKEN     Zendesk API token
    ZENDESK_NOTE_AUTHORS  (optional) comma-separated Zendesk user ids allowed to
                          command it. Unset means any agent or admin on the account,
                          which is already the set of people who can write a private
                          note at all.
    ZENDESK_NOTE_MODEL    (optional) overrides the model
    ZENDESK_HOUSE_ANSWERS (optional) path to the house-answer file — what support
                          usually replied to each kind of problem, per platform.
                          Absent, drafting works exactly as it does without it

Usage:
    note_reply.py --ticket 12345 [--dry-run]
"""
import argparse
import html
import json
import os
import re
import sys
import textwrap

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import reply  # noqa: E402  (needs the path insert above)
import triage  # noqa: E402

DEFAULT_MODEL = "claude-sonnet-5"
COMPOSE_TIMEOUT_SECONDS = 240
PLACEMENT_TIMEOUT_SECONDS = 120

# What Claude is given of the customer's own words, to answer in the right language
# and at the right level of detail.
CUSTOMER_SAMPLE_CHARS = 2000
# A brief is a sentence or two. Anything longer is a pasted reply, which is
# reply.py's job, not this one.
BRIEF_CHARS = 2000

# Status the ticket moves to once the reply is out: the ball is with the customer.
# Same convention as reply.py, so `open` keeps meaning "ours".
REPLIED_STATUS = "pending"
# `claude: solve` sets this. Not "closed": Zendesk refuses closed over the API, and
# the account's own automation closes a solved ticket four days later anyway.
SOLVED_STATUS = "solved"

TAG_QUEUED = "claude-queued"
TAG_DRAFTED = "claude-drafted"
TAG_SENT = "claude-sent"
TAG_SOLVED = "claude-solved"
TAG_ERROR = "claude-error"
# Where a ticket was filed in the taxonomy, cached on the ticket so a second draft
# does not pay for the classification again. Also what a future `group` verb writes.
TAG_GROUP_PREFIX = "grp-"
TAG_PLATFORM_PREFIX = "plat-"

# The house answers: what support actually replied to this kind of problem before,
# per platform. Off unless ZENDESK_HOUSE_ANSWERS points at the file — the drafting
# path works exactly as it did without it.
#
# Deliberately NOT in this repo. It is built from real solved tickets and carries
# their ids and their content, and this repository is public. It belongs beside the
# dedup state on the host, for the same reason that does.
HOUSE_ENV = "ZENDESK_HOUSE_ANSWERS"
PLATFORMS_COLLAPSED = ("android", "ios", "desktop", "multiple", "unknown")

# Wraps the exact text that will be sent. `reply` copies what is between these lines
# and nothing else, so the draft note can carry back-translations and instructions
# around them without any of that reaching the customer. Numbered, because a draft
# offers the agent a choice: `claude: reply 2` sends the second.
BEGIN = "-----BEGIN REPLY"
END = "-----END REPLY"
MAX_OPTIONS = 3


def begin_marker(n):
    return f"{BEGIN} {n}-----"


def end_marker(n):
    return f"{END} {n}-----"


# The number in the closing line has to match the opening one, so a stray delimiter
# inside a reply cannot silently truncate the block that gets sent.
OPTION_BLOCK = re.compile(
    re.escape(BEGIN) + r"\s+(\d+)-----(.*?)" + re.escape(END) + r"\s+\1-----",
    re.DOTALL)


def done_marker(comment_id):
    """Written into the outcome note so a replayed webhook cannot act twice.

    Zendesk retries a webhook that does not answer cleanly, and this runs after the
    comment is already posted — so without this, a slow run means the customer is
    emailed twice. Keyed on the commanding comment because that is what is unique
    per instruction; the ticket id is not.
    """
    return f"[claude:done:{comment_id}]"


def draft_marker(comment_id):
    """Marks a note as carrying a sendable draft, and says which brief produced it."""
    return f"[claude:draft:{comment_id}]"


def english_marker(latest_public_id):
    """Records how far a transcript note read, so re-running is cheap.

    Keyed on the newest public comment it covered rather than on the command: asking
    twice with nothing said in between should cost nothing, and asking again after
    the customer writes back should produce a fresh transcript.
    """
    return f"[claude:english:{latest_public_id}]"


# Anchored to the start of a line so that prose mentioning the command in passing —
# including the instructions in Claude's own draft notes — is not a command.
COMMAND = re.compile(
    r"^[\s>*_]*claude\s*:\s*(draft|reply|english|explain|solve)\b[\s\-–—:.]*(.*)$",
    re.IGNORECASE)


def parse_command(text):
    """(action, brief) from a note, or None when it is not a command.

    The brief runs from the command line to the end of the note, so it can be
    several lines without needing quoting.
    """
    lines = (text or "").splitlines()
    for index, line in enumerate(lines):
        found = COMMAND.match(line)
        if not found:
            continue
        rest = [found.group(2)] + lines[index + 1:]
        return found.group(1).lower(), "\n".join(rest).strip()[:BRIEF_CHARS]
    return None


def comment_text(comment):
    """The note as text, as it was typed.

    `plain_body` because Zendesk stores comments as HTML, and unescaped because
    plain_body strips the tags without touching the entities — an ampersand posted
    as `&amp;` comes back as `&amp;`. Since notes are written escaped, unescaping
    here is what makes the round trip exact, which is what lets `reply` send the
    reviewed draft byte for byte.
    """
    return html.unescape(comment.get("plain_body") or comment.get("body") or "")


# ---- Zendesk ----------------------------------------------------------------


def api_user_id(session, subdomain):
    """The user the API token authenticates as.

    Its own notes are skipped when looking for a command, which is the in-code half
    of the loop guard. The other half is the Zendesk trigger, which should exclude
    this same user so a draft never fires the webhook at all — see the README.
    """
    url = f"https://{subdomain}.zendesk.com/api/v2/users/me.json"
    resp = triage.request_with_retry(session, "GET", url)
    if resp.status_code >= 400:
        sys.exit(f"Zendesk refused to identify the API user ({resp.status_code}).")
    return ((resp.json() or {}).get("user") or {}).get("id")


def fetch_user(session, subdomain, user_id):
    url = f"https://{subdomain}.zendesk.com/api/v2/users/{user_id}.json"
    resp = triage.request_with_retry(session, "GET", url)
    if resp.status_code >= 400:
        return {}
    return (resp.json() or {}).get("user") or {}


def customer_sample(session, subdomain, ticket, comments):
    """The customer's own words, for deciding which language to reply in.

    reply.customer_text takes only comments the REQUESTER authored, which is right
    for email and web tickets. On a Twitter or Sunshine DM the integration authors
    the customer's message under its own id, so that filter drops everything they
    wrote and leaves the ticket's "Conversation with <handle>" description — and the
    reply goes out in English to somebody writing Chinese.

    So: the requester's own words when the ticket carries any, and otherwise every
    public comment written by someone who is not an agent on this account. Roles are
    looked up rather than guessed from the id, because the integration's id is an
    account detail and an unknown author is a customer, not an agent.
    """
    if not triage.is_content_free(ticket):
        return reply.customer_text(ticket, comments)
    roles, parts = {}, []
    subject = triage.squash(ticket.get("subject"))
    for comment in reversed(comments):          # oldest first, so it reads in order
        if not comment.get("public"):
            continue
        author = comment.get("author_id")
        if author not in roles:
            roles[author] = (fetch_user(session, subdomain, author) or {}).get("role")
        if roles[author] in ("agent", "admin"):
            continue
        body = triage.squash(comment.get("body"))
        if body and body != subject:
            parts.append(body)
    return triage.clip("\n\n".join(parts),
                       CUSTOMER_SAMPLE_CHARS) or reply.customer_text(ticket, comments)


def may_command(user):
    """Whether this Zendesk user may drive the command.

    Default is any agent or admin: only they can write a private note in the first
    place, so an allowlist is a narrowing, not the gate. Setting ZENDESK_NOTE_AUTHORS
    narrows it to named ids — and an id that is not on the account fails closed,
    because the role check is applied either way.
    """
    if user.get("role") not in ("agent", "admin"):
        return False
    allowed = [item.strip() for item in
               (os.environ.get("ZENDESK_NOTE_AUTHORS") or "").split(",") if item.strip()]
    return not allowed or str(user.get("id")) in allowed


def change_tags(session, subdomain, ticket_id, add=(), drop=()):
    """Add and remove tags, through the tags sub-resource.

    NOT `additional_tags`/`remove_tags` on the ticket update: those are update_many
    fields. A single-ticket update accepts them with a 200 and silently ignores them,
    which is how every tag this tool set went missing while every call reported
    success. Measured against the live API, not assumed.

    The sub-resource is also additive rather than read-modify-write, so two runs on
    one ticket cannot clobber each other's tags.
    """
    url = f"https://{subdomain}.zendesk.com/api/v2/tickets/{ticket_id}/tags.json"
    for method, names in (("PUT", [t for t in add if t]),
                          ("DELETE", [t for t in drop if t])):
        if not names:
            continue
        resp = triage.request_with_retry(session, method, url, json={"tags": names})
        if resp.status_code >= 400:
            # Never worth failing a run over: tags are a dashboard light, not the work.
            print(f"Note: could not {method.lower()} tags on #{ticket_id} "
                  f"({resp.status_code}).")


def clear_queued(session, subdomain, ticket_id, dry_run=False):
    """Take the ticket out of the "waiting on Claude" queue, writing no comment.

    Every run that finishes servicing a ticket clears it, including one that decides
    there is nothing to do. Otherwise the tag accumulates: Claude's own notes name
    the commands, so posting one re-fires the trigger, and that second run finds only
    its own note and writes nothing — leaving `claude-queued` behind on every ticket
    it ever touched, which is precisely the signal that is supposed to mean a job was
    dropped.
    """
    if dry_run:
        return
    change_tags(session, subdomain, ticket_id, drop=[TAG_QUEUED])


def para(text):
    """One paragraph of the note, escaped."""
    return f"<p>{html.escape(text)}</p>"


def bold_para(text):
    """A paragraph that leads a section — a speaker line in a transcript."""
    return f"<p><strong>{html.escape(text)}</strong></p>"


def transcript_blocks(turns, translated):
    """One turn at a time, as readable paragraphs.

    Deliberately not triage.render_transcript's output re-split on blank lines: a
    turn whose own text contains a blank line gets torn into several pieces that
    way, which is what made the first version render as a row of disconnected code
    boxes. The speaker line leads each turn and the body follows as prose — nothing
    extracts this text, so it wants readability, not byte fidelity.
    """
    english = {}
    for item in translated or []:
        try:
            english[int(item.get("index"))] = (item.get("english") or "").strip()
        except (TypeError, ValueError):
            continue
    out = []
    for turn in turns:
        out.append(bold_para(" ".join(part for part in (turn["when"], f'{turn["who"]}:')
                                      if part)))
        body = english.get(turn["index"]) or turn["body"]
        out += [para(chunk.strip()) for chunk in body.split("\n\n") if chunk.strip()]
    return out


def verbatim(text):
    """A block whose whitespace is preserved exactly.

    `<pre>` rather than paragraphs because this is what `reply` will publish: line
    breaks, blank lines and indentation have to survive the round trip through
    Zendesk unchanged, and paragraph markup silently reflows them.
    """
    return f"<pre>{html.escape(text)}</pre>"


def write_to_ticket(session, subdomain, ticket_id, body, public,
                    status=None, add_tags=(), drop_tags=(), as_html=False):
    """One PUT carrying a comment and any tag or status change.

    `additional_tags`/`remove_tags` rather than writing the whole tag list: two runs
    on one ticket would otherwise race and one would drop the other's tag.

    The response body is never printed. Zendesk echoes the submitted comment back in
    a 422, and that comment is the reply — which this repo's public logs must not
    carry.
    """
    # Notes go as html_body so their structure survives; the public reply goes as
    # plain body, the way reply.py has always sent one — it is prose, not a document.
    fields = {"comment": {("html_body" if as_html else "body"): body, "public": public}}
    if status:
        fields["status"] = status
    url = f"https://{subdomain}.zendesk.com/api/v2/tickets/{ticket_id}.json"
    resp = triage.request_with_retry(session, "PUT", url, json={"ticket": fields})
    if resp.status_code >= 400:
        sys.exit(f"Zendesk rejected the {'reply' if public else 'note'} on "
                 f"#{ticket_id} ({resp.status_code}).")
    # After the comment, so a tag failure cannot lose the thing that mattered.
    change_tags(session, subdomain, ticket_id, add_tags, drop_tags)


# ---- What we usually reply ---------------------------------------------------


def load_house(path=None):
    """The house answers, or None when the feature is off or the file is unusable.

    Degrades rather than fails: a missing or corrupt knowledge file must cost a
    slightly thinner draft, never the ability to answer a customer at all.
    """
    path = path or os.environ.get(HOUSE_ENV)
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            book = json.load(handle)
    except (OSError, ValueError) as exc:
        print(f"Note: could not read the house answers at {path} ({exc}); "
              f"drafting without them.")
        return None
    if not isinstance(book, dict) or not book.get("cells"):
        print(f"Note: {path} carries no house answers; drafting without them.")
        return None
    return book


def tagged_placement(ticket):
    """(group, platform) already recorded on the ticket, or (None, None).

    Read before classifying so a revision, or a ticket a `group` command has already
    filed, does not pay for the same model call twice.
    """
    group = platform = None
    for tag in ticket.get("tags") or []:
        if tag.startswith(TAG_GROUP_PREFIX):
            group = tag[len(TAG_GROUP_PREFIX):]
        elif tag.startswith(TAG_PLATFORM_PREFIX):
            platform = tag[len(TAG_PLATFORM_PREFIX):]
    return group, platform


PLACEMENT_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["group", "platform"],
    "properties": {
        "group": {"type": "string", "description": "a group key from the catalogue, "
                                                   "or 'none' when nothing fits"},
        "platform": {"type": "string", "enum": list(PLATFORMS_COLLAPSED)},
    },
}

PLACEMENT_SYSTEM = textwrap.dedent(
    """
    You file one Zendesk ticket for Session, a private messenger, into an existing
    taxonomy, so that what support usually replies to this kind of problem can be
    looked up.

    `group` is the key whose problem this ticket describes. Judge by what the
    customer needs answered. Answer `none` rather than forcing a fit — a wrong group
    hands the agent someone else's answer, which is worse than handing them none.

    `platform` is the platform the ticket is about. Answer `unknown` unless the text
    actually says: a guessed platform produces per-platform advice that was never
    about this customer's device.
    """
).strip()


def place_ticket(model, book, ticket, sample):
    """Which group and platform this ticket belongs to. (None, None) if unplaceable."""
    catalogue = "\n".join(f"- {g['key']}: {g['title']}" for g in book["groups"])
    body = triage.clip(sample, CUSTOMER_SAMPLE_CHARS)
    try:
        found = triage.claude_cli_json(
            model, "medium", PLACEMENT_SYSTEM, PLACEMENT_SCHEMA,
            f"GROUP CATALOGUE:\n{catalogue}\n\nTHE TICKET:\n"
            f"{(ticket.get('subject') or '')[:200]}\n\n{body}",
            PLACEMENT_TIMEOUT_SECONDS, f"the placement of #{ticket['id']}")
    except SystemExit as exc:
        # Grounding is an enrichment. A failed classification costs a thinner draft,
        # not the draft — the same call triage.py makes about its transcripts.
        print(f"Note: could not place #{ticket['id']} ({exc}); drafting without "
              f"the house answer.")
        return None, None
    group = found.get("group")
    if group == "none" or not any(g["key"] == group for g in book["groups"]):
        return None, found.get("platform")
    return group, found.get("platform")


def house_cell(book, group, platform):
    """The house answer for this group and platform, falling back to all platforms.

    A group with only three solved tickets has no per-platform answer, and the
    all-platform one is still better than nothing. Returns (cell, which_platform).
    """
    if not (book and group):
        return None, None
    for candidate in (platform, "any"):
        cell = book["cells"].get(f"{group}|{candidate}")
        if cell:
            return cell, candidate
    return None, None


def render_precedent(cell, title, platform):
    """The house answer as prompt text.

    Its version numbers and fix claims are deliberately not offered as facts to
    repeat — see the PRECEDENT rules in COMPOSE_SYSTEM. What the model is meant to
    take is the shape: what support covers for this problem, and in what order.
    """
    lines = [f"PROBLEM AS PREVIOUSLY FILED: {title}",
             f"PLATFORM THIS PRECEDENT COVERS: {platform}",
             f"BUILT FROM {cell['n']} SOLVED TICKETS ({cell['consistency']} consistency)",
             "", "WHAT SUPPORT USUALLY SAYS:", cell["answer"]]
    if cell.get("steps"):
        lines += ["", "STEPS USUALLY GIVEN:"] + [f"- {s}" for s in cell["steps"]]
    return "\n".join(lines)


# ---- Composing --------------------------------------------------------------

OPTION_PROPERTIES = {
    "approach": {"type": "string",
                 "description": "What this option does, in English, a few words — "
                                "'explain and close', 'ask which device was online first'. "
                                "It is how the agent tells the options apart."},
    "reply_en": {"type": "string", "description": "The reply, in English."},
    "translated": {"type": "string",
                   "description": "`reply_en` in the customer's language. Identical to "
                                  "`reply_en` when `is_english` is true."},
    "back_translation": {"type": "string",
                         "description": "`translated` rendered literally back into English. "
                                        "Empty when `is_english` is true."},
}
COMPOSE_PROPERTIES = {
    "language": {"type": "string",
                 "description": "Language the customer writes in, in English, e.g. 'German'."},
    "language_code": {"type": "string", "description": "BCP-47 code, e.g. 'de', 'pt-BR'."},
    "is_english": {"type": "boolean",
                   "description": "True only if the customer already writes in English."},
    "options": {
        "type": "array",
        "description": "The candidate replies, best first. Two or three on a first "
                       "draft; usually one when amending.",
        "items": {"type": "object", "additionalProperties": False,
                  "required": list(OPTION_PROPERTIES.keys()),
                  "properties": OPTION_PROPERTIES},
    },
}
COMPOSE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": list(COMPOSE_PROPERTIES.keys()),
    "properties": COMPOSE_PROPERTIES,
}

COMPOSE_SYSTEM = textwrap.dedent(
    """
    You write support replies for Session, a private messenger. A support agent has
    read the ticket and written you a brief — the substance of the answer, in
    shorthand. You turn it into the reply the customer receives.

    Two halves, and they have different rules. The facts are the agent's and you may
    not touch them. The writing is yours and you are expected to do it well.

    FACTS — THE BRIEF IS THE ONLY SOURCE, and the customer's own message.

    - Never state a fact neither of them contains: no version numbers, no dates, no
      timelines, no retention periods, no links, no "our team is working on it". If
      the brief says attachments last 14 days, say 14 days; do not explain the
      mechanism behind it and do not guess what happens after.
    - Never offer a workaround, a next step or a "try this" the brief does not give
      you. This is the rule most often broken by trying to be helpful, and a
      confident wrong instruction costs the customer more than a short answer does.
    - NEVER claim an action was taken unless the brief says it was taken. Do not
      write that an account was banned, a bug was filed, a refund was issued or a
      case was escalated on your own initiative. A reply asserting something nobody
      did is the worst thing you can produce here.
    - If the brief is too thin to answer what they actually asked, answer the part it
      covers and stop. Do not fill the gap.

    WRITING — this half is yours, and a reply that is correct and cold is a worse
    reply. You may and generally should:

    - open by acknowledging what happened to them, in their own terms
    - say plainly that it is frustrating or disappointing, where it plainly is
    - restate their situation back to them, drawn from THEIR message, so they can
      see they were understood
    - draw out what a fact from the brief means for them, where that follows directly
      from it — "so they are no longer on the server" follows from a 14-day limit
    - close by inviting them back if something is still unclear

    None of that introduces a fact, so none of it is forbidden. The line is simple:
    "I'm sorry your photos are gone" is writing. "You can get them back by X" is a
    fact, and needs the brief behind it.

    Do not address the customer by name — you have not been given it. Never emit a
    placeholder like (User name) or [Name]: on this account those have been sent to
    real customers literally, and it is the single most visible way a reply looks
    machine-made.

    `language` and `language_code` describe the language the CUSTOMER writes in,
    judged from their words alone, never the agent's brief.

    OPTIONS. Return two or three, and make them GENUINELY DIFFERENT — different
    decisions about how to handle the ticket, not the same reply reworded. The useful
    axes are usually: answer and close it; ask for the one detail that is missing
    before committing to an answer; answer but keep it open in case they come back.
    Order them best first, and let `approach` say in a few words what each one does,
    so the agent can choose without reading all three in full.

    If the brief only supports one honest reply, return one. Padding the list with a
    variant nobody would pick wastes the agent's reading, which is the whole thing
    this is meant to save.

    `reply_en` is the reply in English: plain and courteous, the way a support agent
    writes to someone they want to help — not stiff, not effusive, not corporate.
    Short paragraphs, blank line between them.

    `translated` is `reply_en` in the customer's language, carrying the meaning across
    exactly. Leave product names, version numbers, URLs, file paths, error strings and
    Session IDs (66 hex characters beginning 05) exactly as they are. When
    `is_english` is true, repeat `reply_en` back unchanged.

    `back_translation` is `translated` rendered back into English, literally. Someone
    who does not speak the language reads it to see what the customer will actually
    receive, so translate what is there rather than what was meant. Do not repair it
    and do not copy `reply_en` — an error introduced by the translation has to survive
    into the back-translation or this step is worthless. Leave it empty when
    `is_english` is true.

    AMENDING. When you are given A PREVIOUS DRAFT, the brief is a change to it, not a
    replacement, and you normally return ONE option — the agent has already chosen.
    Return more only when the brief actually asks for alternatives. The agent has already read the rest and kept it, so keep it too:
    change only what the brief asks for, add what it adds, and leave every other
    sentence alone. Rewriting what they already approved makes them review it twice.

    PRECEDENT. When you are given WHAT SUPPORT USUALLY SAYS, it is a record of how
    this kind of ticket has been answered before. It is not a fact source and it is
    not an instruction. Read it for SHAPE — which points get covered, in what order,
    at what length — and for the generic troubleshooting steps it lists.

    Take nothing specific from it. Not a version number, not a date, not "fixed in",
    not "a fix is coming", not a claim that anything was escalated, filed or banned.
    Those were true of some other ticket on some other day, and several of them have
    since turned out to be wrong. If the agent wants one of them in this reply, the
    agent will put it in the brief.

    Where the precedent and the brief disagree, THE BRIEF WINS AND THE PRECEDENT IS
    DROPPED. The agent read this ticket; the precedent did not.
    """
).strip()


def build_compose_prompt(sample, brief, previous=None, precedent=None):
    """The customer's words, the brief, and — where they exist — the draft being
    amended and the precedent for this kind of ticket.

    Order matters twice over. Precedent comes first because it is background; the
    brief comes last because it is the instruction, and what is nearest the end is
    what governs.
    """
    parts = []
    if precedent:
        parts += [precedent, ""]
    parts += ["THE CUSTOMER'S OWN WORDS FROM THE TICKET:", sample]
    if previous:
        parts += ["", "A PREVIOUS DRAFT, WHICH THE AGENT IS AMENDING:", previous,
                  "", "THE AGENT'S BRIEF, AS A CHANGE TO THAT DRAFT:", brief]
    else:
        parts += ["", "THE AGENT'S BRIEF FOR THE REPLY:", brief]
    return "\n".join(parts)


def validate_composition(result):
    """Exit unless there is text to review. Structured output guarantees the keys;
    this is about the values, since an empty option would offer the agent a blank
    to send."""
    if not isinstance(result, dict):
        sys.exit(f"Claude returned {type(result).__name__}, expected an object.")
    missing = [key for key in COMPOSE_PROPERTIES if key not in result]
    if missing:
        sys.exit(f"Claude's draft is missing: {', '.join(missing)}.")
    options = [o for o in (result.get("options") or [])
               if isinstance(o, dict) and (o.get("translated") or "").strip()]
    if not options:
        sys.exit("Claude returned no usable reply option.")
    result["options"] = options[:MAX_OPTIONS]
    return result


def compose(model, sample, brief, previous=None, precedent=None):
    return validate_composition(triage.claude_cli_json(
        model, "medium", COMPOSE_SYSTEM, COMPOSE_SCHEMA,
        build_compose_prompt(sample, brief, previous, precedent),
        COMPOSE_TIMEOUT_SECONDS, "the reply draft"))


# ---- Notes ------------------------------------------------------------------


def build_draft_note(result, brief, comment_id, amended=False,
                     cell=None, title=None, covering=None):
    """The note the agent reviews, as HTML.

    Each option sits in its own numbered verbatim block: those are what `reply` will
    publish, so their whitespace has to survive Zendesk unchanged. Everything around
    them is prose and gets paragraphs.

    The instructions name the commands, so they are written mid-line on purpose:
    COMMAND only matches at the start of a line, so this note cannot command itself
    even if the trigger is misconfigured.
    """
    options = result["options"]
    what = "revised the reply" if amended else "drafted a reply"
    lead = (f"Claude {what}. It will be sent in {result['language']}.") if len(options) == 1 \
        else (f"Claude {what} — {len(options)} options, in {result['language']}. "
              f"Pick one with a private note reading claude: reply 2.")
    out = [para(lead)]
    for number, option in enumerate(options, 1):
        out.append(para(f"Option {number} — {option.get('approach') or 'reply'}"))
        out += [para(begin_marker(number)), verbatim(option["translated"].strip()),
                para(end_marker(number))]
        if not result["is_english"] and (option.get("back_translation") or "").strip():
            out += [para(f"Option {number}, back in English:"),
                    verbatim(option["back_translation"].strip())]
    out += [para("Brief this was written from:" if not amended
                 else "Change this revision was asked for:"),
            verbatim(brief.strip())]
    if cell:
        out.append(para(f"Shaped by what we usually reply to: {title} ({covering}) — "
                        f"{cell['n']} solved tickets, {cell['consistency']} consistency."))
        if cell.get("examples"):
            out.append(para("Past tickets: " + ", ".join(f"#{i}" for i in cell["examples"])))
        if (cell.get("caveat") or "").strip():
            out.append(para("Careful, from those past replies: " + cell["caveat"].strip()))
    send = "add a private note — claude: reply" if len(options) == 1 \
        else "add a private note — claude: reply <the option number>"
    out += [para(f"To send it exactly as above, {send}"),
            para("To change it, add a private note — claude: draft <a new brief>"),
            para(f"{done_marker(comment_id)} {draft_marker(comment_id)}")]
    return "".join(out)


def find_draft(comments, api_user):
    """The options carried by the newest draft note, as {number: text}. {} if none.

    Newest wins: an agent who did not like the first draft writes a new brief, and
    `reply` must offer what they last looked at. Restricted to notes this tool wrote,
    so a human pasting the delimiters into a note of their own cannot smuggle text
    past the review.

    reply.fetch_comments returns newest first, so this walks the list as it comes.
    """
    for comment in comments:
        if comment.get("public") or comment.get("author_id") != api_user:
            continue
        found = {int(number): body.strip()
                 for number, body in OPTION_BLOCK.findall(comment_text(comment))
                 if body.strip()}
        if found:
            return found
    return {}


def choose_option(options, asked):
    """(text, complaint) for the option the agent asked for.

    A bare `claude: reply` sends the only option there is. With more than one it
    refuses and lists them rather than guessing: picking for them would send a
    customer a reply they did not choose.
    """
    numbers = sorted(options)
    if asked is None:
        if len(numbers) == 1:
            return options[numbers[0]], None
        return None, ("This draft offers " + str(len(numbers)) + " options ("
                      + ", ".join(str(n) for n in numbers) + "). Say which one, "
                      "like — claude: reply " + str(numbers[0]))
    if asked not in options:
        return None, (f"There is no option {asked} on this draft. It offers "
                      + ", ".join(str(n) for n in numbers) + ".")
    return options[asked], None


OPTION_NUMBER = re.compile(r"^\s*#?(\d+)\b")


def asked_option(brief):
    """The option number in `claude: reply 2`, or None for a bare `claude: reply`."""
    found = OPTION_NUMBER.match(brief or "")
    return int(found.group(1)) if found else None


def first_line(text, limit=90):
    """A short quotation of what went out, for the audit note.

    Enough to recognise the draft that was sent when two were drafted; never the
    whole reply, which is already on the ticket one comment above.
    """
    line = next((part.strip() for part in (text or "").splitlines() if part.strip()), "")
    return triage.clip(line, limit)


def build_sent_note(user, comment_id, sent):
    return "".join([
        para(f"Reply sent by {user}, from their note on this ticket."),
        para(f'Sent draft beginning: "{first_line(sent)}"'),
        para(done_marker(comment_id)),
    ])


# ---- Actions ----------------------------------------------------------------


def run_draft(session, subdomain, model, ticket, comments, command, api_user, dry_run):
    """Compose the reply, or revise the one already on the ticket.

    A second `draft` on a ticket that already has one is an amendment, not a fresh
    start: an agent writing "also mention X" wants the draft they just read plus X,
    and regenerating from the new brief alone would throw away the wording they kept.
    """
    ticket_id = ticket["id"]
    comment_id, brief = command["id"], command["brief"]
    if not brief:
        say(session, subdomain, ticket_id, comment_id,
            "The brief was empty, so there was nothing to write. Add a private note "
            "like — claude: draft <what the answer is>", dry_run)
        return
    shown = find_draft(comments, api_user)
    previous = "\n\n".join(f"Option {n}:\n{shown[n]}" for n in sorted(shown)) or None

    sample = customer_sample(session, subdomain, ticket, comments)
    book = load_house()
    group, platform = tagged_placement(ticket)
    new_tags = []
    if book and not group:
        group, platform = place_ticket(model, book, ticket, sample)
        # Cached on the ticket so a revision does not pay for the same call again,
        # and so the placement is visible to a human who disagrees with it.
        new_tags = ([f"{TAG_GROUP_PREFIX}{group}"] if group else []) + \
                   ([f"{TAG_PLATFORM_PREFIX}{platform}"] if platform else [])
    cell, covering = house_cell(book, group, platform)
    title = next((g["title"] for g in (book or {}).get("groups", [])
                  if g["key"] == group), group)
    precedent = render_precedent(cell, title, covering) if cell else None
    if cell:
        print(f"#{ticket_id}: grounded in {group}/{covering} "
              f"({cell['n']} solved, {cell['consistency']} consistency).")

    result = compose(model, triage.clip(sample, CUSTOMER_SAMPLE_CHARS), brief,
                     previous, precedent)
    print(f"#{ticket_id}: {'revised' if previous else 'drafted'} "
          f"{len(result['options'])} option(s); requester writes "
          f"{result['language']!r} ({result['language_code']}).")
    if dry_run:
        print(f"#{ticket_id}: dry run, nothing written.")
        return
    write_to_ticket(session, subdomain, ticket_id,
                    build_draft_note(result, brief, comment_id, bool(previous),
                                     cell, title, covering),
                    public=False, as_html=True,
                    add_tags=[TAG_DRAFTED] + new_tags,
                    drop_tags=[TAG_QUEUED, TAG_ERROR])


def run_solve(session, subdomain, ticket, command, dry_run):
    """Solve the ticket without writing anything to the customer.

    For the ones that need no reply — spam, an abuse report with nothing actionable,
    a duplicate, a question already answered elsewhere. There were 197 of those in
    the backlog when this was written.

    The private note is the point: a status change on its own leaves nothing on the
    ticket saying who decided that or why, which is exactly what somebody reopening
    it in three months needs to know.
    """
    ticket_id = ticket["id"]
    if ticket.get("status") == SOLVED_STATUS:
        say(session, subdomain, ticket_id, command["id"],
            "This ticket is already solved.", dry_run, error=False)
        return
    author = fetch_user(session, subdomain, command["author"])
    who = author.get("name") or f"user {command['author']}"
    if dry_run:
        print(f"#{ticket_id}: dry run, would solve on behalf of {who}.")
        return
    note = [para(f"Solved by {who}, from their note on this ticket. "
                 f"No reply was sent to the customer.")]
    if command["brief"]:
        note.append(para("Reason given: " + command["brief"]))
    note.append(para(done_marker(command["id"])))
    write_to_ticket(session, subdomain, ticket_id, "".join(note), public=False,
                    status=SOLVED_STATUS, as_html=True, add_tags=[TAG_SOLVED],
                    drop_tags=[TAG_QUEUED, TAG_ERROR])
    print(f"#{ticket_id}: solved, no reply sent.")


def run_explain(session, subdomain, model, ticket, comments, command, dry_run):
    """Say what we already know about this kind of ticket, without writing a reply.

    The house answer, the steps, and — the reason this verb exists — what was
    actually DONE before: fixes confirmed shipped, bugs filed, escalations. Those are
    the things `draft` deliberately refuses to put in a reply, because they were true
    of another ticket on another day. Here they are shown to a human, who can decide
    whether one still applies and put it in the brief.
    """
    ticket_id = ticket["id"]
    book = load_house()
    if not book:
        say(session, subdomain, ticket_id, command["id"],
            "No house answers are configured on this relay, so there is nothing to "
            "look up.", dry_run)
        return
    group, platform = tagged_placement(ticket)
    new_tags = []
    if not group:
        group, platform = place_ticket(
            model, book, ticket, customer_sample(session, subdomain, ticket, comments))
        new_tags = ([f"{TAG_GROUP_PREFIX}{group}"] if group else []) + \
                   ([f"{TAG_PLATFORM_PREFIX}{platform}"] if platform else [])
    cell, covering = house_cell(book, group, platform)
    if not cell:
        say(session, subdomain, ticket_id, command["id"],
            "This ticket does not match anything in the house answers, so there is "
            "no precedent to show. Write the brief yourself.", dry_run, error=False)
        return
    title = next((g["title"] for g in book["groups"] if g["key"] == group), group)
    print(f"#{ticket_id}: {group}/{covering}, {cell['n']} solved.")
    if dry_run:
        print(f"#{ticket_id}: dry run, nothing written.")
        return
    out = [para(f"What we usually reply to: {title} ({covering}) — "
                f"{cell['n']} solved tickets, {cell['consistency']} consistency."),
           para(cell["answer"])]
    if cell.get("steps"):
        out.append(para("Steps usually given: " + "; ".join(cell["steps"])))
    if cell.get("actions"):
        out.append(bold_para("What was actually done on those tickets:"))
        out += [para(action) for action in cell["actions"]]
    if (cell.get("caveat") or "").strip():
        out.append(para("Careful: " + cell["caveat"].strip()))
    if cell.get("examples"):
        out.append(para("Verify against " + ", ".join(f"#{i}" for i in cell["examples"])))
    out.append(para("Nothing was written to the customer. To answer, add a private "
                    "note — claude: draft <what the answer is>"))
    out.append(para(done_marker(command["id"])))
    write_to_ticket(session, subdomain, ticket_id, "".join(out), public=False,
                    as_html=True, add_tags=new_tags, drop_tags=[TAG_QUEUED, TAG_ERROR])


def run_reply(session, subdomain, ticket, comments, command, api_user, dry_run):
    """Publish the chosen option, exactly as it was reviewed.

    It never re-composes. What the agent read in the draft note is what the customer
    receives, or the review step is theatre — so a change means a new brief, not a
    different send.
    """
    ticket_id = ticket["id"]
    comment_id = command["id"]
    options = find_draft(comments, api_user)
    if not options:
        say(session, subdomain, ticket_id, comment_id,
            "There is no draft on this ticket to send. Add a private note like — "
            "claude: draft <what the answer is>", dry_run)
        return
    sending, complaint = choose_option(options, asked_option(command["brief"]))
    if complaint:
        say(session, subdomain, ticket_id, comment_id, complaint, dry_run, error=False)
        return
    author = fetch_user(session, subdomain, command["author"])
    who = author.get("name") or f"user {command['author']}"
    if dry_run:
        print(f"#{ticket_id}: dry run, would send an option on behalf of {who}.")
        return
    write_to_ticket(session, subdomain, ticket_id, sending, public=True,
                    status=REPLIED_STATUS)
    write_to_ticket(session, subdomain, ticket_id,
                    build_sent_note(who, comment_id, sending), public=False,
                    as_html=True, add_tags=[TAG_SENT],
                    drop_tags=[TAG_QUEUED, TAG_DRAFTED, TAG_ERROR])
    print(f"#{ticket_id}: reply sent and status -> {REPLIED_STATUS}.")


def already_english(turns, translated):
    """Whether the translation came back as the text it was given.

    Asked after the call rather than guessed before it. A character test looked
    cheaper, but "Hallo, ich habe ein Problem" is pure ASCII — it would have called
    every unaccented German ticket English and left the agent unable to read the
    thing they asked to read. Judging by the output costs one model call and cannot
    make that mistake.

    Any turn the model did not return, or returned changed, means translating
    happened — so this fails towards posting the transcript.
    """
    english = {}
    for item in translated or []:
        try:
            english[int(item.get("index"))] = (item.get("english") or "").strip()
        except (TypeError, ValueError):
            continue
    squash = lambda text: " ".join((text or "").split()).lower()
    return all(english.get(turn["index"])
               and squash(english[turn["index"]]) == squash(turn["body"])
               for turn in turns)


def run_english(session, subdomain, model, ticket, comments, command, dry_run):
    """Put the conversation on the ticket in English, as a private note.

    Reads both sides, not just the customer's: their second message is usually an
    answer to a reply, and without the reply it reads as a complaint about nothing.

    Python owns the speaker labels and timestamps and the model only translates —
    the same split triage.py makes, for the same reason: a model asked to format the
    transcript can drop a turn, merge two, or date one it was never given, and each
    of those is invisible in the output.
    """
    ticket_id = ticket["id"]
    latest = next((c.get("id") for c in comments if c.get("public")), None)
    if latest is not None and reply.already_replied(comments, english_marker(latest)):
        say(session, subdomain, ticket_id, command["id"],
            "The English transcript on this ticket is already up to date — nothing "
            "has been said since it was written.", dry_run, error=False)
        return
    turns = triage.conversation_turns(session, subdomain, ticket)
    if not turns:
        say(session, subdomain, ticket_id, command["id"],
            "There are no public comments on this ticket to translate.", dry_run)
        return

    payload = json.dumps([{"index": t["index"], "speaker": t["who"], "text": t["body"]}
                          for t in turns], ensure_ascii=False)
    rendered = triage.claude_cli_json(
        model, "medium", triage.TRANSCRIPT_SYSTEM_PROMPT, triage.TRANSCRIPT_SCHEMA,
        triage.clip(payload, triage.TRANSCRIPT_INPUT_CHARS),
        triage.ENGLISH_TIMEOUT_SECONDS, f"the English transcript of #{ticket_id}")
    if already_english(turns, rendered.get("turns")):
        # A transcript of English text repeats what is already a few comments above
        # it. Say so rather than posting the same words back.
        say(session, subdomain, ticket_id, command["id"],
            "This conversation is already in English, so there is nothing to "
            "translate.", dry_run, error=False)
        return
    print(f"#{ticket_id}: rendered {len(turns)} turn(s) in English.")
    if dry_run:
        print(f"#{ticket_id}: dry run, nothing written.")
        return
    note = "".join(
        [para(f"This conversation in English — {len(turns)} turn(s), both sides."),
         para("Translated for reading; the customer has not seen this.")]
        + transcript_blocks(turns, rendered.get("turns"))
        + [para(f"{done_marker(command['id'])} {english_marker(latest)}")])
    write_to_ticket(session, subdomain, ticket_id, note, public=False, as_html=True,
                    drop_tags=[TAG_QUEUED, TAG_ERROR])


def say(session, subdomain, ticket_id, comment_id, text, dry_run, error=True):
    """Answer the agent on the ticket, and record that this command was handled.

    Every refusal carries the done marker: a command that cannot be satisfied is
    still a command that was answered, and without the marker a webhook retry would
    ask again and post the same complaint a second time.

    `error=False` for an answer that is not a failure — "there is nothing new to
    translate" is the command working. Tagging that `claude-error` would put a
    working ticket in the queue of broken ones.
    """
    print(f"#{ticket_id}: {text.splitlines()[0]}")
    if dry_run:
        return
    write_to_ticket(session, subdomain, ticket_id,
                    para(text) + para(done_marker(comment_id)), public=False,
                    as_html=True, add_tags=[TAG_ERROR] if error else [],
                    drop_tags=[TAG_QUEUED] + ([] if error else [TAG_ERROR]))


# ---- Entry point ------------------------------------------------------------


def latest_command(comments, api_user, session, subdomain):
    """The newest private note that is a command from someone allowed to give one.

    reply.fetch_comments returns newest first, so the first match is the newest
    command: older ones have already been handled and carry their own done markers.

    An unauthorised author stops the search rather than falling through to an older
    command. Their note is the most recent instruction on the ticket, and quietly
    acting on a previous one instead would be a surprising thing to do.
    """
    for comment in comments:
        if comment.get("public") or comment.get("author_id") == api_user:
            continue
        parsed = parse_command(comment_text(comment))
        if not parsed:
            continue
        user = fetch_user(session, subdomain, comment.get("author_id"))
        if not may_command(user):
            print(f"Ignoring a command from {user.get('role') or 'an unknown user'}.")
            return None
        return {"id": comment.get("id"), "author": comment.get("author_id"),
                "action": parsed[0], "brief": parsed[1]}
    return None


def main():
    parser = argparse.ArgumentParser(description="Act on claude: notes on a Zendesk ticket.")
    parser.add_argument("--ticket", type=int, required=True)
    parser.add_argument("--model", default=os.environ.get("ZENDESK_NOTE_MODEL", DEFAULT_MODEL))
    parser.add_argument("--dry-run", action="store_true",
                        help="do everything except write to Zendesk")
    args = parser.parse_args()

    subdomain = triage.get_env("ZENDESK_SUBDOMAIN")
    session = triage.zendesk_session(triage.get_env("ZENDESK_EMAIL"),
                                     triage.get_env("ZENDESK_API_TOKEN"))
    api_user = api_user_id(session, subdomain)

    ticket = reply.fetch_ticket(session, subdomain, args.ticket)
    if ticket.get("status") == "closed":
        # Closed is irreversible and takes no comments at all, so there is nowhere to
        # even report the refusal. Say it to the journal and stop.
        print(f"#{args.ticket}: closed, so Zendesk takes no comments. Nothing done.")
        return
    comments = reply.fetch_comments(session, subdomain, args.ticket)

    command = latest_command(comments, api_user, session, subdomain)
    if not command:
        print(f"#{args.ticket}: no command note to act on.")
        clear_queued(session, subdomain, args.ticket, args.dry_run)
        return
    if reply.already_replied(comments, done_marker(command["id"])):
        print(f"#{args.ticket}: this command was already handled; nothing written.")
        clear_queued(session, subdomain, args.ticket, args.dry_run)
        return

    if command["action"] == "draft":
        run_draft(session, subdomain, args.model, ticket, comments, command,
                  api_user, args.dry_run)
    elif command["action"] == "solve":
        run_solve(session, subdomain, ticket, command, args.dry_run)
    elif command["action"] == "explain":
        run_explain(session, subdomain, args.model, ticket, comments, command,
                    args.dry_run)
    elif command["action"] == "english":
        run_english(session, subdomain, args.model, ticket, comments, command, args.dry_run)
    else:
        run_reply(session, subdomain, ticket, comments, command, api_user, args.dry_run)


if __name__ == "__main__":
    main()
