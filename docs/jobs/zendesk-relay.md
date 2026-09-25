# Zendesk Reply from a Private Note

> Replying used to be possible from the digest, behind a **Comment** button on each
> card that opened a compose dialog in Discord. That is gone: the digest is read-only
> now and the ticket is the only place a reply is written. Removing it took with it
> `reply.py`, the `/discord/interactions` route, the Ed25519 signature check, and the
> `DISCORD_PUBLIC_KEY` / `ALLOWED_USER_IDS` / `ALLOWED_ROLE_IDS` / `DISCORD_GUILD_ID`
> settings — one reply path instead of two, with one set of semantics.

| | |
| --- | --- |
| Runs | `zendesk-relay.service`, always on, behind nginx at `POST /zendesk/notes` |
| Secrets | `/etc/zendesk/env`: `ZENDESK_WEBHOOK_SECRET` plus the digest's Zendesk and Claude credentials |
| Dry run | `RELAY_DRY_RUN=1` in the env file; `uv run zendesk-note-reply --ticket N --dry-run` |
| Re-run | `zendesk-note-reply --ticket N` under the relay's environment; a command already answered carries `[claude:done:<comment id>]` and is not answered twice |
| Logs | `journalctl -u zendesk-relay -n 50 --no-pager` |

The Discord path answered one ticket from the digest, and no longer exists. This one
answers a ticket from inside Zendesk, where the queue is actually worked: an agent
writes a private note saying what the answer is, Claude writes it properly in the
requester's language, and the agent sends it with a second note.

```
claude: draft - attachments are only kept on the server for 14 days. A second
        device that was offline for longer cannot fetch them.
```

Claude replies with a private note carrying the drafted reply, a back-translation,
and the brief it was written from. A draft usually offers two or three genuinely
different approaches, numbered, so the agent reads:

```
claude: reply 2
```

which publishes that option **verbatim** and moves the ticket to `pending`. A bare
`claude: reply` sends the only option when there is one, and refuses to guess when
there are several.

## The commands

| Command | What it does | Touches the customer |
| --- | --- | --- |
| `claude: draft - <brief>` | Compose the reply from the brief, in the requester's language. A second `draft` amends the one already there rather than starting over | no |
| `claude: reply [n]` | Publish the chosen option verbatim, status -> `pending` | **yes** |
| `claude: english` | Post the conversation, both sides, in English. Says so and writes nothing when the ticket is already English | no |
| `claude: explain` | Post what support usually replied to this kind of ticket, what was actually done about it, and the caveats | no |
| `claude: solve [reason]` | Solve without writing to the customer, for tickets that need no reply. The note records who decided and why | no comment, but **solving fires the CSAT automation** |

## Why a draft is always reviewed

The reply flow this replaced sent an English ticket immediately, because the agent had
typed the exact words and there was nothing to check. Here Claude *composes* the reply
from a brief, so nobody has read that wording yet — every draft is reviewed,
English included. `reply` never re-composes: what was reviewed is what goes out, or
the review means nothing. To change a draft, write a new brief.

## What stops it drafting against itself

Claude's own draft note names both commands in its instructions. If those parsed as
commands, every draft would trigger another one, forever. Two independent guards:

- `COMMAND` only matches at the **start of a line**, and the instructions in a draft
  note are written mid-line on purpose. `test_a_generated_draft_note_is_not_a_command`
  asserts it.
- The command search skips notes authored by the API user, and the Zendesk trigger
  should exclude that same user so a draft never reaches the webhook at all.

Give the automation its own Zendesk user rather than reusing an account a human signs
into — otherwise excluding it in the trigger also excludes that person's notes, and
the tool silently stops working for them.

## Who may command it

Only private comments count, so a customer typing `claude:` into a public reply is
ignored. The author must be an agent or admin — the set of people who can write a
private note at all. `ZENDESK_NOTE_AUTHORS` narrows that to named user ids; the role
check still applies, so an id on the list that is not an agent is still refused.

An unauthorised author **stops** the search rather than falling through to an older
command. Their note is the most recent instruction on the ticket, and quietly acting
on a previous one instead would be a surprising thing to do.

## What the model may write

The brief is the only source of facts. The system prompt forbids adding a version
number, a date, a retention period, a link or a timeline the brief does not contain —
and forbids claiming an action was taken unless the brief says it was. That second
rule is the important one: 183 solved tickets in this account tell a reporter their
Account ID "has been banned from communities we operate", and a reply asserting
something nobody did is the worst thing this can produce. Both rules are asserted by
`test_the_prompt_forbids_inventing_facts_and_actions`, so a prompt edit cannot
quietly drop them.

## Idempotency

Zendesk retries a webhook that does not answer cleanly, and the reply is written
before the run finishes — so without a guard, a slow run emails the customer twice.
Every outcome note carries `[claude:done:<comment id>]`, keyed on the commanding
comment rather than the ticket, because two briefs on one ticket are two commands and
the second must not be swallowed by the first one's marker. Refusals carry it too: a
command that cannot be satisfied is still a command that was answered.

## Tags

| Tag | Set by | Cleared by |
| --- | --- | --- |
| `claude-queued` | the Zendesk trigger, when the note lands | a successful run |
| `claude-drafted` | a draft being posted | the reply going out |
| `claude-sent` | the reply going out | — |
| `claude-solved` | `claude: solve`, on every solve | — |
| `claude-error` | a refusal or a failed Claude call, with the reason in the note | the next successful run |

The tag is the durable queue and the webhook is only a latency optimisation. A relay
that is down leaves `claude-queued` on the ticket, so `tags:claude-queued` older than
a few minutes is the list of dropped jobs — a webhook-only design would lose them
silently. Two views are worth making: `tags:claude-queued` for what did not run, and
`tags:claude-drafted` for what is waiting on a human. `claude-sent` and
`claude-solved` are never cleared: they are the record of what this tool did, and
`tags:claude-solved` is how a bulk solve is found again and reopened.

## Zendesk setup

A trigger, and a webhook it calls:

- **Webhook** — POST to `https://<host>/zendesk/notes`, JSON body `{"ticket_id":
  "{{ticket.id}}"}`, signed. Put the signing secret in `ZENDESK_WEBHOOK_SECRET`;
  without it the route refuses everything, because a URL that writes to customers
  must not default to open.
- **Trigger** — conditions: *Ticket is Updated*, *Comment is Private*, *Comment text
  contains `claude:`*, and *Current user is not* the automation user. Actions: notify
  the webhook, and add the tag `claude-queued`.

## Required Secrets

| Secret | Description |
| --- | --- |
| `ZENDESK_WEBHOOK_SECRET` | Shared secret Zendesk signs the webhook with. Unset refuses every request |
| `ZENDESK_NOTE_AUTHORS` | *(optional)* Comma-separated Zendesk user ids allowed to command it. Unset means any agent or admin |
| `ZENDESK_NOTE_MODEL` | *(optional)* Overrides the model |

The Zendesk credentials and Claude authentication are the ones the digest already
uses. `RELAY_DRY_RUN` covers this path too: the whole run happens and nothing is
written.

## Local Testing

```
# what the webhook does, against a real ticket, writing nothing
uv run zendesk-note-reply --ticket 27603 --dry-run
```

A ticket with no command note prints `no command note to act on` and stops, so this
is safe to point at anything.
