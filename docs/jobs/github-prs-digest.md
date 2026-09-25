# Contributor Pull Requests

[`digest.py`](../../src/session_ops/github_prs/digest.py) posts one message each weekday morning
listing the open pull requests in session-foundation's repositories whose author is not
a maintainer and which have moved in the last three days — the ones nobody on the team
has a reason to already know about:

| | |
| --- | --- |
| Runs | `github-prs-digest.timer`, Mon–Fri 09:30 Australia/Melbourne |
| Secrets | `/etc/github-prs/env`: `GITHUB_PRS_TOKEN` with no scopes at all, the channel's webhook, `ALERT_DISCORD_WEBHOOK_URL` |
| Dry run | `uv run github-prs-digest --dry-run` |
| Re-run | `systemctl start github-prs-digest.service` |
| Logs | `journalctl -u github-prs-digest -n 50 --no-pager` |

```
**Contributor pull requests** · last 3 days
🟢 **2** new · ✏️ **1** updated
**36** open from contributors across the org.

**session-desktop**
🟢 [#1958](…) @KyWB · 12h · 💬1 · Fix issue #563
✏️ [#1904](…) @scrense-hash · 3h · 💬2 · feat: add SOCKS5 proxy support
```

🟢 is a PR the digest has never reported; ✏️ is one it has, which has moved since. A PR
that has not moved is left out entirely, however wide the window. The backlog line
counts every open contributor PR regardless, so a quiet day still says how much is
waiting.

## Weekdays, and the state file

The timer runs `Mon..Fri`, so Monday's run has to cover the weekend — hence a 72-hour
window rather than a daily one. That window overlaps itself by two days on every run,
and [`--state`](../../src/session_ops/github_prs/digest.py) is what stops the overlap being noise: it records
which PRs reached Discord and what each one's `updated_at` was at the time.

`updated_at` moves on *any* change, including one that touches several PRs at once — a
label sweep, a base branch renamed — so those resurface once even though nobody worked
on them. The accurate alternative is the head SHA and the comment counts, which are not
in the search result and cost a request per PR that moved; this is the cheaper half of
that trade, taken deliberately.

Only what Discord accepted is recorded, so a run that fails on its second message
re-reports that message's PRs tomorrow rather than losing them. Every way of failing to
read the state file — missing, unreadable, written by another version — treats every PR
in the window as new: noisy once, never wrong, which is what makes it a cache rather
than something to back up.

One search fetches every open PR in the org and the window is applied to the result
here rather than in the query — that is what buys the backlog count for the cost of a
single query. Past GitHub's 1000-result search ceiling the digest says the counts are a
floor instead of failing.

## Who is a maintainer

[`maintainers.txt`](../../src/session_ops/github_prs/maintainers.txt), one login per line, matched
case-insensitively. Bot accounts need no entry — every account GitHub types as a `Bot`
is dropped, so a renamed Dependabot stays out on its own.

Neither of the two things GitHub could answer this with is a substitute. Org membership
covers six accounts, two of which are not in the review loop; push access is held by a
dozen more as outside collaborators, several of them contractors whose PRs are exactly
what the digest is for. Both would get it wrong in both directions, so the list is
written by hand — and goes stale silently, since a new maintainer's PRs are reported as
a stranger's until someone adds them.

## What is left out

Forks, archived repositories and private repositories, by checking the search results
against the org's repository list rather than by name — so a repository created today
is covered today and a fork of an upstream project never is. There is no flag to widen
that: private repositories stay out whatever the token can see.

| flag | |
| --- | --- |
| `--window-hours N` | how far back a PR must have moved to be considered (default 72) |
| `--state PATH` | dedup state; without it every PR in the window is new |
| `--state-retention-days N` | drop state entries older than this (default 30) |
| `--dry-run` | print the Discord payload, post nothing |
| `--org`, `--token`, `--webhook` | override the environment |
| `--maintainers PATH` | a different list |

| env var | |
| --- | --- |
| `GITHUB_PRS_TOKEN` | read-only token; no scope at all is needed, the digest reads public repositories only |
| `GITHUB_PRS_DISCORD_WEBHOOK_URL` | the channel it posts to (not needed with `--dry-run`) |
| `GITHUB_PRS_ORG` | optional; defaults to `session-foundation` |

It runs on the same box as the Zendesk digest, under its own user and its own
environment file — see [deploy/README.md](../../deploy/README.md). Its HTTP retries,
Discord posting and dedup state are the same code the Zendesk digest uses, in
[shared/](../../src/session_ops/shared/).

```sh
uv run python -m unittest discover -s tests/github_prs -t .
uv run python -m unittest discover -s tests/shared -t .
```
