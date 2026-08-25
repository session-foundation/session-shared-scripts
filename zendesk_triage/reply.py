#!/usr/bin/env python3
"""
Reply to a Zendesk ticket from Discord, in the language the ticket was written in.

The triage digest is where the team reads tickets, so it should be where they answer
one. A Comment button on a digest card opens a dialog, the agent writes in English,
Claude translates into the language the requester writes in, and the reply lands on
the ticket as a public comment.

    ⚠️  This writes public comments to Zendesk, which emails the requester. Nothing
        it does is reversible. --dry-run does everything except the two writes.

Two actions, both driven by a Discord interaction that relay.py verified before
handing it here:

  draft  the modal was submitted. Reads the ticket, detects the language the
         requester writes in, translates, and then either sends straight away — an
         English ticket has no translation to review — or renders a preview
         carrying a back-translation and Send/Cancel buttons.
  send   Send was pressed. The preview's embeds carry the translated text and the
         English original, so nothing had to be stored between the two actions.

What lands on the ticket:

  * a public comment carrying the reply, and status -> pending
  * a private note naming the Discord author, plus the English original when it
    differs from what the customer received. On an English ticket that would be the
    same text twice, so there the note is the attribution line alone.

Both are authored by ZENDESK_EMAIL: an API token authenticates as exactly one agent,
so who sent the reply is recorded in the note rather than in the comment's byline.

Because this repo is public, nothing here prints ticket content, reply text, or
translations. The draft goes to an ephemeral Discord message and the reply goes to
Zendesk; stdout gets ticket ids and outcomes.

Config (env vars, or flags for local runs):
    ZENDESK_SUBDOMAIN     e.g. "mycompany"  -> https://mycompany.zendesk.com
    ZENDESK_EMAIL         agent email for API token auth; authors every comment
    ZENDESK_API_TOKEN     Zendesk API token
                          Translation runs through the locally installed Claude
                          Code CLI, which supplies its own credentials, so this
                          script holds no Claude key.
    DISCORD_PAYLOAD       the relayed interaction, as JSON (see load_payload)
    ZENDESK_REPLY_MODEL   (optional) Claude model id or alias; defaults to
                          claude-sonnet-5. Translating a paragraph is easy and an
                          agent is watching a spinner while it happens, so this tier
                          is picked for latency — unlike the nightly digest, which
                          pins opus for batch-wide clustering.

Usage:
    # what relay.py does: the whole interaction arrives in DISCORD_PAYLOAD
    python reply.py

    # everything except the two writes to Zendesk
    python reply.py --dry-run

    # driven by a hand-written payload, touching neither Zendesk nor Discord.
    # --local refuses to run without --dry-run, because alone it still writes.
    python reply.py --dry-run --local
"""
import argparse
import json
import os
import sys
import textwrap

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import triage  # noqa: E402  (needs the path insert above)

# Sonnet rather than the digest's pinned opus: this runs while someone watches a
# spinner, and translating one paragraph does not need the tier that clusters 45
# tickets by root cause. Overridable by ZENDESK_REPLY_MODEL through the same alias
# table the triage uses.
DEFAULT_MODEL = "claude-sonnet-5"
# Generous for one short reply, and the only bound on the call now that it goes
# through the CLI. An agent is watching a spinner, but Discord holds the dialog
# open, so waiting beats failing.
TRANSLATION_TIMEOUT_SECONDS = 180
# Status after a public reply: the ball is with the customer. This bumps updated_at,
# so the ticket reappears in the next digest carrying the 🔄 "changed since last
# reported" marker — which is the truth about it, not noise.
REPLIED_STATUS = "pending"
# How much of the requester's writing to send as the language signal. It takes a
# sentence to tell German from Dutch; the rest is tokens spent on a decision that
# was already made.
CUSTOMER_SAMPLE_CHARS = 1500
# Discord's per-embed description cap, and the cap on all embed content in one
# message. Both matter because the preview's embeds are also the draft's only
# storage — see build_preview.
MAX_EMBED_CHARS = 4096
MAX_EMBEDS_TOTAL_CHARS = 6000
# Floor on what the back-translation gets of the shared budget. It is the only
# reason the confirmation step is worth taking, so a draft that would squeeze it
# down to a line is refused rather than shown with the check gutted.
MIN_BACK_TRANSLATION_CHARS = 200
# Model output, so it can be any length; embed titles cap at 256.
LANGUAGE_LABEL_CHARS = 40
# The preview's three titles, defined once because they are both rendered and
# measured. Discord's 6,000 counts titles and footers as well as descriptions, so
# these come off the budget — and a fixed guess at their length would silently
# understate it the moment one of them is edited.
TITLE_TRANSLATED = "Will be sent, in {language}"
TITLE_BACK_TRANSLATION = "…which says, back in English"
TITLE_ORIGINAL = "You wrote"

BLURPLE = 0x5865F2
GREY = 0x4E5058
# Discord component styles: 1 primary, 2 secondary, 3 success, 4 danger. Send is the
# affirmative action rather than the destructive one, so it is green and Cancel is
# plain grey — red on Cancel would point the warning at the harmless button.
BUTTON_SUCCESS = 3
BUTTON_SECONDARY = 2


# ---- The draft, carried in the preview message ------------------------------
#
# A component interaction hands back the message the component was attached to, so
# the preview's own embeds are where the draft lives between the modal and the Send
# button. Nothing is stored anywhere: no cache to lose, no row to expire, and a
# preview from a run whose state was thrown away still works.
#
# Positional, and pinned by a round-trip test. The footer marker is what makes a
# preview from an older deploy fail loudly instead of being mis-parsed.
PREVIEW_VERSION = 1
EMBED_TRANSLATED = 0
EMBED_BACK_TRANSLATION = 1
EMBED_ORIGINAL = 2


def preview_marker(ticket_id):
    """Footer text tying a preview to one ticket and one rendering of this file."""
    return f"zdr:{PREVIEW_VERSION}:{ticket_id}"


def sent_marker(interaction_id):
    """The line in the private note that makes a re-run idempotent.

    Interaction ids are unique per click, so a second run over the same interaction
    finds its own marker and stops. Discord's own guard is upstream — the relay strips
    the buttons in the same response that acknowledges the click, so a double-click
    cannot get this far — and this covers the case that guard cannot: the same
    interaction being replayed after it already wrote.
    """
    return f"[discord:{interaction_id}]"


def preview_language(result):
    """The language label exactly as the preview renders it.

    Shared so the title and the budget cannot disagree about how long it is.
    """
    return triage.clip(result.get("language"), LANGUAGE_LABEL_CHARS) or "another language"


def preview_chrome(result, ticket_id):
    """Embed text a preview spends before any of the draft goes in.

    The three titles and the footer marker, measured from the same constants
    build_preview renders — so the longest language label and the longest ticket id
    are accounted for rather than assumed. A fixed figure held only while nobody
    edited a title and ticket ids stayed short, and said so nowhere.
    """
    return (len(TITLE_TRANSLATED.format(language=preview_language(result)))
            + len(TITLE_BACK_TRANSLATION)
            + len(TITLE_ORIGINAL)
            + len(preview_marker(ticket_id)))


def back_translation_budget(result, body_en, ticket_id):
    """Characters left for the back-translation once everything fixed is in.

    The translation and the agent's own words have to survive the round trip
    unaltered; the back-translation is only ever read inside Discord, so it is the
    one that gives ground when the message runs out of room.
    """
    return min(MAX_EMBED_CHARS,
               MAX_EMBEDS_TOTAL_CHARS - preview_chrome(result, ticket_id)
               - len(result["translated"]) - len(body_en))


def round_trip_error(result, body_en, ticket_id):
    """Why this draft cannot survive a round trip through Discord, or None.

    The Send button reads the translated text back out of the embed, so a
    description Discord truncated would post a truncated reply to a customer.
    Refusing here is the only way to keep "what was reviewed" and "what was sent"
    the same text.
    """
    translated = result["translated"]
    for label, text in (("translation", translated), ("reply", body_en)):
        if len(text) > MAX_EMBED_CHARS:
            return (f"The {label} came to {len(text):,} characters, past Discord's "
                    f"{MAX_EMBED_CHARS:,}-character limit for one block. "
                    f"Send it as a shorter reply.")
    if back_translation_budget(result, body_en, ticket_id) < MIN_BACK_TRANSLATION_CHARS:
        return (f"The reply and its translation come to "
                f"{len(translated) + len(body_en):,} characters, leaving no room to "
                f"show you what the translation says back. Send it as a shorter "
                f"reply.")
    return None


def build_preview(subdomain, ticket_id, result, body_en):
    """The ephemeral message the agent confirms.

    Three blocks in the order the decision is made: what the customer will read,
    what that says back in English, and what the agent actually wrote. The middle
    one is the point of the whole step — it is how someone who does not speak the
    language can tell whether the translation drifted.
    """
    language = preview_language(result)
    url = triage.ticket_url(subdomain, ticket_id)
    return {
        "content": (f"Reply to [#{ticket_id}]({url}) in **{language}**. Check the "
                    f"back-translation before sending — this emails the requester "
                    f"and cannot be taken back."),
        "embeds": [
            {
                "title": TITLE_TRANSLATED.format(language=language),
                "description": result["translated"],
                "color": BLURPLE,
                "footer": {"text": preview_marker(ticket_id)},
            },
            {
                "title": TITLE_BACK_TRANSLATION,
                "description": triage.clip(
                    result["back_translation"],
                    back_translation_budget(result, body_en, ticket_id)),
                "color": GREY,
            },
            {
                "title": TITLE_ORIGINAL,
                "description": body_en,
                "color": GREY,
            },
        ],
        "components": [{
            "type": 1,
            "components": [
                {"type": 2, "style": BUTTON_SUCCESS, "label": "Send",
                 "custom_id": f"send:{ticket_id}"},
                {"type": 2, "style": BUTTON_SECONDARY, "label": "Cancel",
                 "custom_id": "cancel"},
            ],
        }],
    }


def parse_preview(embeds, ticket_id):
    """Recover (translated, body_en) from the embeds of a preview message.

    The footer is checked against the ticket the button named, so a Send pressed on
    some other message cannot post this text to the wrong ticket — the one failure
    mode that keeping the draft in the message rather than in a store would open up.
    """
    if not isinstance(embeds, list) or len(embeds) <= EMBED_ORIGINAL:
        sys.exit(f"#{ticket_id}: the message behind Send is not a reply preview.")
    blocks = [block if isinstance(block, dict) else {} for block in embeds]
    footer = (blocks[EMBED_TRANSLATED].get("footer") or {}).get("text") or ""
    if footer != preview_marker(ticket_id):
        sys.exit(f"#{ticket_id}: this preview belongs to a different ticket or an "
                 f"older version of the reply script; write the reply again.")
    translated = blocks[EMBED_TRANSLATED].get("description") or ""
    body_en = blocks[EMBED_ORIGINAL].get("description") or ""
    if not translated:
        sys.exit(f"#{ticket_id}: the preview carries no text to send.")
    return translated, body_en


def build_note(user, interaction_id, body_en, translated):
    """The private note: who sent the reply, and what they meant by it.

    The English original is included only when it is not already the public comment.
    On an English ticket the two are the same text, and a note repeating the comment
    one line below it is noise in a ticket someone will read later. The attribution
    and the marker are on both paths — every reply stays traceable to a person, and
    idempotency works the same way whatever the language.
    """
    lines = [f"Reply sent from Discord by {user}."]
    if body_en.strip() and body_en.strip() != translated.strip():
        lines += ["", "Original (English):", body_en]
    lines += ["", sent_marker(interaction_id)]
    return "\n".join(lines)


def outcome_message(subdomain, ticket_id, text):
    """Replace the preview with a one-line outcome.

    Empty embeds and components, always: leaving them behind would keep a live Send
    button under a message that says the reply already went out.
    """
    return {
        "content": f"{text} · [#{ticket_id}]({triage.ticket_url(subdomain, ticket_id)})",
        "embeds": [],
        "components": [],
    }


def respond(payload, data):
    """Edit the message the agent is looking at. Exits if Discord will not take it.

    A fresh session, never the Zendesk one — that carries the API-token auth header,
    and Discord has no business receiving it.

    No bot token: the interaction token that arrived in the payload is the
    credential for this endpoint, which is why nothing on this side of the relay
    holds a Discord secret.
    """
    # --local, for a run driven by a hand-written payload whose interaction token is
    # not real. It prints the reply and its translation, so nothing that runs by
    # itself may pass it: relay.run_reply builds the only command line production
    # uses, and --dry-run is the only flag it can add.
    if payload.get("local"):
        print("Discord would receive:\n"
              + json.dumps(data, indent=2, ensure_ascii=False))
        return
    url = (f"https://discord.com/api/v10/webhooks/{payload['application_id']}"
           f"/{payload['interaction_token']}/messages/@original")
    resp = triage.request_with_retry(requests.Session(), "PATCH", url, json=data)
    if resp.status_code >= 400:
        # Interaction tokens expire 15 minutes after the click, so a run that queued
        # for a long time can find the message unreachable. Whatever was written to
        # Zendesk is already correct; fail the run anyway, because an agent who
        # never saw an outcome will assume the reply did not go out.
        sys.exit(f"Discord rejected the response ({resp.status_code}); the "
                 f"interaction token may have expired. Check the ticket in Zendesk "
                 f"before writing the reply again.")


# ---- Zendesk ----------------------------------------------------------------


def fetch_ticket(session, subdomain, ticket_id):
    url = f"https://{subdomain}.zendesk.com/api/v2/tickets/{ticket_id}.json"
    resp = triage.request_with_retry(session, "GET", url)
    if resp.status_code == 404:
        sys.exit(f"Ticket #{ticket_id} does not exist.")
    if resp.status_code >= 400:
        # A read error's body describes the error, not the ticket, so it is safe to
        # print here. The write path deliberately prints no body at all.
        sys.exit(f"Could not read ticket #{ticket_id} ({resp.status_code}): "
                 f"{resp.text[:200]}")
    return (resp.json() or {}).get("ticket") or {}


def fetch_comments(session, subdomain, ticket_id):
    """The ticket's comments, newest first.

    Newest first because the marker that stops a re-run from writing twice will be
    on the most recent comment, and one page of a busy ticket would otherwise be all
    opening back-and-forth. The language sample leads with the description instead,
    which the ticket object already carries.
    """
    url = f"https://{subdomain}.zendesk.com/api/v2/tickets/{ticket_id}/comments.json"
    resp = triage.request_with_retry(
        session, "GET", url, params={"per_page": 100, "sort_order": "desc"})
    if resp.status_code >= 400:
        sys.exit(f"Could not read the comments on #{ticket_id} ({resp.status_code}).")
    return (resp.json() or {}).get("comments") or []


def customer_text(ticket, comments):
    """What the requester wrote, as the signal for which language to reply in.

    The requester's own comments only. An agent's earlier English reply on the same
    ticket is still text on the ticket, and including it would drag detection towards
    English on exactly the tickets this feature exists for.
    """
    requester_id = ticket.get("requester_id")
    parts = []
    description = (ticket.get("description") or "").strip()
    if description:
        parts.append(description)
    for comment in comments:
        if comment.get("author_id") != requester_id:
            continue
        body = (comment.get("body") or "").strip()
        if body and body not in parts:
            parts.append(body)
    # A ticket can carry no text at all — an attachment, or an import that lost its
    # body. Say so rather than sending an empty sample, which reads as a blank
    # question the model has to answer anyway.
    return triage.clip("\n\n".join(parts), CUSTOMER_SAMPLE_CHARS) or "(no text)"


def already_replied(comments, marker):
    return any(marker in (comment.get("body") or "") for comment in comments)


def post_comment(session, subdomain, ticket_id, body, public, status=None):
    """Add one comment to a ticket. One update carries one comment, so the public
    reply and the private note are two calls.

    The response body is never printed, unlike the read path: Zendesk echoes the
    submitted value back in a 422, and that value is the reply — which this repo's
    public run logs must not carry.
    """
    url = f"https://{subdomain}.zendesk.com/api/v2/tickets/{ticket_id}.json"
    fields = {"comment": {"body": body, "public": public}}
    if status:
        fields["status"] = status
    resp = triage.request_with_retry(session, "PUT", url, json={"ticket": fields})
    if resp.status_code >= 400:
        sys.exit(f"Zendesk rejected the {'reply' if public else 'private note'} on "
                 f"#{ticket_id} ({resp.status_code}).")


# ---- Translation ------------------------------------------------------------

TRANSLATION_PROPERTIES = {
    "language": {
        "type": "string",
        "description": "Language the customer writes in, in English, e.g. 'German'.",
    },
    "language_code": {
        "type": "string",
        "description": "BCP-47 code for that language, e.g. 'de', 'pt-BR'.",
    },
    "is_english": {
        "type": "boolean",
        "description": "True only if the customer already writes in English.",
    },
    "translated": {
        "type": "string",
        "description": "The agent's reply in the customer's language.",
    },
    "back_translation": {
        "type": "string",
        "description": "`translated` rendered literally back into English.",
    },
}
TRANSLATION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": list(TRANSLATION_PROPERTIES.keys()),
    "properties": TRANSLATION_PROPERTIES,
}

TRANSLATION_SYSTEM_PROMPT = textwrap.dedent(
    """
    You translate a support agent's reply so that a Zendesk customer can read it in
    their own language. You are given the customer's own words from the ticket, and
    the agent's reply written in English.

    - `language` and `language_code` describe the language the CUSTOMER writes in,
      never the agent's. Judge it from the customer's text alone.
    - `is_english` is true only when the customer already writes in English.
    - `translated` is the agent's reply in the customer's language. Carry the
      meaning across exactly. Add nothing the agent did not write — no greeting, no
      sign-off, no apology, no offer of further help. Leave product names, version
      numbers, URLs, file paths, error strings and Session IDs (66 hex characters
      beginning 05) exactly as they are. Write it the way a support agent writes in
      that language: plain and courteous, not stiff. If `is_english` is true, repeat
      the agent's reply back unchanged.
    - `back_translation` is `translated` rendered back into English, literally. It
      is read by someone who does not speak the language and needs to see what the
      customer will actually read, so translate what is there rather than what was
      meant. Do not repair it, do not smooth it over, and do not copy the agent's
      original wording — an error introduced by the translation has to survive into
      the back-translation or this step is worthless.
    """
).strip()


def build_translation_prompt(sample, reply_en):
    return (
        "THE CUSTOMER'S OWN WORDS FROM THE TICKET:\n"
        f"{sample}\n\n"
        "THE AGENT'S REPLY, IN ENGLISH:\n"
        f"{reply_en}"
    )


def validate_translation(result):
    """Exit unless the payload carries text to send.

    Structured outputs guarantee the keys, so this is about the values: an empty
    `translated` would otherwise post an empty comment to a customer.
    """
    if not isinstance(result, dict):
        sys.exit(f"Claude returned {type(result).__name__}, expected an object.")
    missing = [key for key in TRANSLATION_PROPERTIES if key not in result]
    if missing:
        sys.exit(f"Claude's translation is missing: {', '.join(missing)}.")
    if not (result["translated"] or "").strip():
        sys.exit("Claude returned an empty translation.")
    return result


def translate(model, sample, reply_en):
    """Detect the customer's language and render the reply into it.

    Through the same Claude Code CLI the digest classifies with, so the deployment
    carries one set of Claude credentials rather than a key here and a login there.
    It costs a few seconds of process startup per call, which the SDK did not — but
    Discord holds the interaction open, so the agent sees a spinner, not a failure.
    """
    return validate_translation(triage.claude_cli_json(
        model, "medium", TRANSLATION_SYSTEM_PROMPT, TRANSLATION_SCHEMA,
        build_translation_prompt(sample, reply_en), TRANSLATION_TIMEOUT_SECONDS,
        "the reply translation"))


# ---- Actions ----------------------------------------------------------------


def deliver(session, subdomain, payload, translated, body_en, comments, dry_run):
    """Write the reply and the note, then report the outcome back to Discord."""
    ticket_id = payload["ticket_id"]
    marker = sent_marker(payload["interaction_id"])
    if already_replied(comments, marker):
        print(f"#{ticket_id}: this interaction already replied; nothing written.")
        respond(payload, outcome_message(
            subdomain, ticket_id, "✅ Already sent — this reply is on the ticket"))
        return
    if dry_run:
        print(f"#{ticket_id}: dry run, nothing written.")
        respond(payload, outcome_message(
            subdomain, ticket_id, "🧪 Dry run — the reply was **not** sent"))
        return

    # The private note goes first, because it carries the marker. Public-first looks
    # more natural and is wrong: if the note then failed, the ticket would hold a
    # reply with no marker, and a re-run would find nothing and email the customer a
    # second time. This way the worst case is an orphan note and no reply — visible
    # in the ticket, and a re-run refuses rather than double-sending. The note is
    # private, so it cannot reach the customer on its own.
    post_comment(session, subdomain, ticket_id,
                 build_note(payload["user"], payload["interaction_id"], body_en,
                            translated),
                 public=False)
    post_comment(session, subdomain, ticket_id, translated, public=True,
                 status=REPLIED_STATUS)
    print(f"#{ticket_id}: reply posted, status set to {REPLIED_STATUS}.")
    respond(payload, outcome_message(subdomain, ticket_id, "✅ Sent"))


def refuse_if_closed(subdomain, payload, ticket):
    """A closed ticket takes no comments at all, and closing is irreversible, so
    there is nothing to do but say so. Returns whether the run should stop."""
    if ticket.get("status") != "closed":
        return False
    ticket_id = payload["ticket_id"]
    print(f"#{ticket_id}: closed, so Zendesk will not take a comment.")
    respond(payload, outcome_message(
        subdomain, ticket_id,
        "❌ Closed, and Zendesk takes no comments on a closed ticket"))
    return True


def run_draft(session, subdomain, model, payload, dry_run):
    ticket_id = payload["ticket_id"]
    reply_en = payload["body_en"]
    ticket = fetch_ticket(session, subdomain, ticket_id)
    if refuse_if_closed(subdomain, payload, ticket):
        return
    comments = fetch_comments(session, subdomain, ticket_id)

    result = translate(model, customer_text(ticket, comments), reply_en)
    print(f"#{ticket_id}: requester writes {result['language']!r} "
          f"({result['language_code']}).")

    if result["is_english"]:
        # Nothing to review, so nothing to confirm. What goes out is the agent's own
        # text and never the model's echo of it: an English reply must reach the
        # customer exactly as it was typed.
        deliver(session, subdomain, payload, reply_en, reply_en, comments, dry_run)
        return

    problem = round_trip_error(result, reply_en, ticket_id)
    if problem:
        print(f"#{ticket_id}: draft too long for a preview.")
        respond(payload, outcome_message(subdomain, ticket_id, f"❌ {problem}"))
        return
    respond(payload, build_preview(subdomain, ticket_id, result, reply_en))
    print(f"#{ticket_id}: preview sent, waiting on the agent.")


def run_send(session, subdomain, payload, dry_run):
    ticket_id = payload["ticket_id"]
    translated, body_en = parse_preview(payload.get("embeds"), ticket_id)
    ticket = fetch_ticket(session, subdomain, ticket_id)
    # Re-checked rather than trusted from the draft: minutes can pass while a
    # preview sits unanswered, and the ticket can be closed in between.
    if refuse_if_closed(subdomain, payload, ticket):
        return
    comments = fetch_comments(session, subdomain, ticket_id)
    deliver(session, subdomain, payload, translated, body_en, comments, dry_run)


ACTIONS = {"draft", "send"}
# Present on every relayed interaction. `body_en` and `embeds` are per-action and
# checked by the action that needs them.
PAYLOAD_KEYS = ("action", "ticket_id", "user", "interaction_id", "application_id",
                "interaction_token")


def load_payload(raw):
    """Parse the relayed interaction, or exit naming what is wrong with it.

    Everything downstream indexes this dict, so a malformed relay should say so here
    rather than surface as a KeyError halfway through a Zendesk write.
    """
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        sys.exit(f"DISCORD_PAYLOAD is not valid JSON: {exc}")
    if not isinstance(payload, dict):
        sys.exit(f"DISCORD_PAYLOAD is {type(payload).__name__}, expected an object.")
    missing = [key for key in PAYLOAD_KEYS if not payload.get(key)]
    if missing:
        sys.exit(f"DISCORD_PAYLOAD is missing: {', '.join(missing)}.")
    if payload["action"] not in ACTIONS:
        sys.exit(f"Unknown action {payload['action']!r}; expected one of "
                 f"{', '.join(sorted(ACTIONS))}.")
    try:
        payload["ticket_id"] = int(payload["ticket_id"])
    except (TypeError, ValueError):
        sys.exit(f"ticket_id {payload['ticket_id']!r} is not a ticket number.")
    if payload["action"] == "draft" and not (payload.get("body_en") or "").strip():
        sys.exit("A draft carries no reply text.")
    return payload


def main():
    parser = argparse.ArgumentParser(
        description="Reply to a Zendesk ticket from Discord, translated.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Do everything except write to Zendesk. Prints no "
                             "ticket content, so it is safe to use in CI.")
    parser.add_argument("--local", action="store_true",
                        help="Print what would go back to Discord instead of "
                             "sending it, for a run driven by a hand-written "
                             "payload. Prints the reply and its translation, so "
                             "local runs only. Requires --dry-run.")
    parser.add_argument("--payload",
                        help="The relayed interaction as JSON (else DISCORD_PAYLOAD).")
    parser.add_argument("--subdomain", help="Zendesk subdomain (else ZENDESK_SUBDOMAIN).")
    parser.add_argument("--email", help="Zendesk agent email (else ZENDESK_EMAIL).")
    parser.add_argument("--api-token", help="Zendesk API token (else ZENDESK_API_TOKEN).")
    parser.add_argument("--model",
                        help=f"Claude model id or alias (else ZENDESK_REPLY_MODEL, "
                             f"default {DEFAULT_MODEL}).")
    args = parser.parse_args()
    # --local only redirects what goes back to Discord. On its own it still posts a
    # public comment, which is the opposite of what a hand-written payload is for —
    # and the mistake costs a real customer a real email.
    if args.local and not args.dry_run:
        parser.error("--local needs --dry-run: by itself it still writes to Zendesk.")

    payload = load_payload(triage.get_env("DISCORD_PAYLOAD", args.payload))
    # The payload is this run's context and every path already carries it, so the
    # output mode rides along rather than being threaded through four signatures.
    payload["local"] = args.local
    subdomain = triage.get_env("ZENDESK_SUBDOMAIN", args.subdomain)
    email = triage.get_env("ZENDESK_EMAIL", args.email)
    api_token = triage.get_env("ZENDESK_API_TOKEN", args.api_token)
    model = triage.resolve_api_model(
        args.model or os.environ.get("ZENDESK_REPLY_MODEL") or DEFAULT_MODEL)

    session = triage.zendesk_session(email, api_token)
    if payload["action"] == "draft":
        run_draft(session, subdomain, model, payload, args.dry_run)
    else:
        run_send(session, subdomain, payload, args.dry_run)


if __name__ == "__main__":
    main()
