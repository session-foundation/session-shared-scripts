"""Components V2 messages over a plain incoming webhook.

A digest is a Container of Text Displays: one block per entry, so a reader skims
lines rather than a wall, and each message records which entries it accounts for.

Every component here is non-interactive, which is what lets a plain incoming
webhook carry it: Discord allows a webhook that no application owns only those.
Adding an interactive one would need the transport moved to a bot token.
https://docs.discord.com/developers/components/reference
"""
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests

from shared.retry import request_with_retry

COMPONENTS_V2_FLAG = 1 << 15
CONTAINER = 17
TEXT_DISPLAY = 10
SEPARATOR = 14
# Discord's ceiling on all the text in one Components V2 message.
MAX_MESSAGE_TEXT_CHARS = 4000


def clip(text, limit):
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def text_display(content):
    return {"type": TEXT_DISPLAY, "content": content}


def separator():
    return {"type": SEPARATOR}


def container_message(blocks):
    return {"flags": COMPONENTS_V2_FLAG,
            "components": [{"type": CONTAINER, "components": blocks}]}


def chunk_entries(entries, max_items, max_chars=MAX_MESSAGE_TEXT_CHARS, first_used=0):
    """Group (text, ids) pairs into messages within Discord's budgets.

    Whichever limit binds first splits the message. Entries are separate components
    rather than joined text, so nothing is spent on the newlines between them.

    `first_used` is what the caller has already spent on the first message before
    any entry goes in: the header. Without it the header rides on top of a full
    budget of entries and a busy day's first message goes over.

    An entry longer than the character budget still gets its own message rather
    than being dropped; callers pre-clip so that should not arise.

    Entries are passed through, not rebuilt, so a caller can still find one by
    identity inside a chunk.
    """
    chunks, current, current_chars = [], [], first_used
    for entry in entries:
        text, _ = entry
        if current and (len(current) >= max_items
                        or current_chars + len(text) > max_chars):
            chunks.append(current)
            current, current_chars = [], 0
        current.append(entry)
        current_chars += len(text)
    if current:
        chunks.append(current)
    return chunks


def messages_from_entries(header, entries, max_items, max_chars=MAX_MESSAGE_TEXT_CHARS):
    """Return (messages, coverage): the header leads message one, entries follow.

    coverage[i] is the union of the ids message i carries, so a run that fails
    partway can record exactly what reached Discord. Message one's header is not
    credited with any ids; a caller whose header accounts for entries adds them.

    A quiet day still owes the channel its header, so no entries yields one
    message.
    """
    messages, coverage = [], []
    chunks = chunk_entries(entries, max_items, max_chars, first_used=len(header)) or [[]]
    for index, chunk in enumerate(chunks):
        blocks = []
        if index == 0:
            blocks.append(text_display(header))
            if chunk:
                blocks.append(separator())
        blocks += [text_display(text) for text, _ in chunk]
        messages.append(container_message(blocks))
        coverage.append(set().union(*(ids for _, ids in chunk)) if chunk else set())
    return messages, coverage


def components_webhook_url(webhook_url):
    """The webhook, told to respect the components field, which it ignores without."""
    parts = urlsplit(webhook_url)
    query = dict(parse_qsl(parts.query))
    query["with_components"] = "true"
    return urlunsplit(parts._replace(query=urlencode(query)))


def post_to_discord(session, url, messages):
    """POST each message in order; return how many Discord accepted.

    Stops at the first failure and returns the count instead of exiting, so the
    caller can record what did land before signalling the failure. Letting the
    exhausted retry propagate would skip that recording, and the messages that
    landed would be reposted on the next run.
    """
    for index, payload in enumerate(messages):
        try:
            resp = request_with_retry(session, "POST", url, json=payload)
        except requests.RequestException as exc:
            print(f"Discord unreachable on message {index + 1}/{len(messages)} "
                  f"({exc}).")
            return index
        if resp.status_code >= 400:
            print(f"Discord rejected message {index + 1}/{len(messages)} "
                  f"({resp.status_code}): {resp.text[:300]}")
            return index
    return len(messages)
