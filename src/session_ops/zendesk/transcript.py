"""The English transcript the digest writes onto a non-English ticket.
"""
import json

import requests

from session_ops.shared.discord import clip
from session_ops.zendesk.api import conversation_turns
from session_ops.zendesk.claude_cli import claude_cli_json


# Optional, and absent until the field exists in Zendesk. Everything below is a
# no-op without it: the digest posts exactly as it did before and relay.py falls
# back to the ticket's own comments, which is what it showed all along.
ENGLISH_FIELD_ENV = "ZENDESK_ENGLISH_FIELD_ID"
ENGLISH_TIMEOUT_SECONDS = 180


# The transcript is a whole conversation rather than one description, so both budgets
# are larger than the classifier's. The field holds well past the 1,200 relay.py
# shows, so the field is never why the dialog is missing a sentence.
TRANSCRIPT_INPUT_CHARS = 8000
TRANSCRIPT_CHARS = 12000


TRANSCRIPT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["turns"],
    "properties": {
        "turns": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["index", "english"],
                "properties": {
                    "index": {
                        "type": "integer",
                        "description": "The turn's index, echoed back unchanged.",
                    },
                    "english": {
                        "type": "string",
                        "description": "That turn in English, or the original text unchanged if it was already English.",
                    },
                },
            },
        }
    },
}

TRANSCRIPT_SYSTEM_PROMPT = (
    "You translate support conversations into English for an agent who does not read "
    "the original language.\n\n"
    "You are given the turns of one ticket as JSON, each with an index. Return one "
    "object per input turn, echoing its index back unchanged.\n\n"
    "Translate faithfully and completely. Keep the speaker's meaning, their order of "
    "events and their tone — an angry turn must still read as angry. Do not "
    "summarise, do not answer, do not merge turns, do not add notes of your own.\n\n"
    "A turn already in English is returned unchanged, word for word. Do not "
    "paraphrase it and do not 'improve' it.\n\n"
    "Leave Session IDs, version numbers, URLs and error strings exactly as written."
)


def is_english(finding):
    """Whether the classifier called this ticket English.

    Unknown counts as English: the field is only worth writing when it says
    something the agent cannot already read, and a blank `language` is far more
    likely to be a classification that came back thin than a ticket nobody could
    read. Guessing wrong this way costs a transcript nobody needed; the other way
    puts a machine translation over the top of words everyone could already read.
    """
    language = (finding.get("language") or "").strip().lower()
    return not language or language.startswith(("english", "en"))


def render_transcript(turns, translated):
    """Turns plus their translations as the text that goes on the ticket.

    Python owns the timestamps and the speaker labels rather than the model. Asked to
    format the transcript itself, a model can drop a turn, merge two, or date one it
    was never given — and every one of those is invisible in the output. Translating
    is the only part that needs a model, so it is the only part it is given.

    A turn the model did not return keeps its original text. Untranslated is a
    degraded transcript; missing is a conversation that reads as if it never happened.
    """
    english = {}
    for item in translated or []:
        try:
            english[int(item.get("index"))] = (item.get("english") or "").strip()
        except (TypeError, ValueError):
            continue
    blocks = []
    for turn in turns:
        header = " ".join(part for part in (turn["when"], f'{turn["who"]}:') if part)
        blocks.append(f'{header}\n{english.get(turn["index"]) or turn["body"]}')
    return "\n\n".join(blocks)


def write_english_field(session, subdomain, ticket_id, field_id, english):
    """Put the transcript on the ticket. Returns whether Zendesk took it.

    One field overwritten, not a note appended: a ticket carries one current English
    version of the whole conversation rather than a chain of partial ones to read in
    order.
    """
    url = f"https://{subdomain}.zendesk.com/api/v2/tickets/{ticket_id}.json"
    payload = {"ticket": {"custom_fields": [{"id": field_id, "value": english}]}}
    try:
        resp = session.request("PUT", url, attempts=2, json=payload)
    except requests.RequestException as exc:
        print(f"Note: could not write the English transcript to #{ticket_id} ({exc}).")
        return False
    if resp.status_code >= 400:
        print(f"Note: #{ticket_id} rejected the English transcript "
              f"({resp.status_code}).")
        return False
    return True


def attach_english(session, subdomain, tickets, findings, model, field_id):
    """Render every non-English ticket about to be posted into English, on the ticket.

    Runs before the digest is posted, and that order is the whole design: the Comment
    button exists only on a digest card, so a ticket that reaches the dialog has
    necessarily been through here first. relay.py can then read the field it needs
    without a Claude call of its own — which it has no time for, being on the three
    seconds Discord allows a dialog that cannot be deferred.

    Scoped to the tickets that actually get a button. Translating the rest would be
    paying for every ticket in the window to serve the handful anybody replies to.

    Never raises: this is enrichment, and a digest that fails to post because a
    translation failed would be a worse trade than a dialog showing German.
    """
    if not session:
        return 0
    if not field_id:
        print(f"No {ENGLISH_FIELD_ENV} set; no English transcripts written.")
        return 0
    candidates = [f for f in findings if not is_english(f)]
    if not candidates:
        print(f"No non-English tickets among the {len(findings)} being posted; "
              f"no English transcripts to write.")
        return 0
    by_id = {t.get("id"): t for t in tickets}
    written = 0
    for finding in candidates:
        ticket = by_id.get(finding.get("id"))
        if not ticket:
            # --findings, or a fetch that returned the classification but not the row.
            print(f"Note: #{finding.get('id')} was classified {finding.get('language')!r} "
                  f"but never fetched; no transcript.")
            continue
        turns = conversation_turns(session, subdomain, ticket)
        if not turns:
            print(f"Note: #{ticket['id']} has no public comments to render.")
            continue
        payload = json.dumps(
            [{"index": t["index"], "speaker": t["who"], "text": t["body"]}
             for t in turns], ensure_ascii=False)
        try:
            rendered = claude_cli_json(
                model, "medium", TRANSCRIPT_SYSTEM_PROMPT, TRANSCRIPT_SCHEMA,
                clip(payload, TRANSCRIPT_INPUT_CHARS), ENGLISH_TIMEOUT_SECONDS,
                f"the English transcript of #{ticket['id']}")
        except SystemExit as exc:
            # claude_cli_json exits on a failed call, which is right for the
            # classification it was written for and wrong here: one ticket nobody
            # can translate must not take the digest down with it.
            print(f"Note: could not render #{ticket['id']} in English ({exc}).")
            continue
        english = clip(render_transcript(turns, rendered.get("turns")),
                       TRANSCRIPT_CHARS)
        if english and write_english_field(session, subdomain, ticket["id"],
                                           field_id, english):
            written += 1
    # Printed even at zero. A run that wrote nothing and a run that never reached
    # this step read identically in the journal otherwise, which is the one thing
    # somebody checking whether the feature is on actually needs to tell apart.
    print(f"Wrote an English transcript to {written} of {len(candidates)} "
          f"non-English ticket(s).")
    return written
