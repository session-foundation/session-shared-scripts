# Zendesk Ticket Triage

Claude reviews the Zendesk tickets awaiting a reply — `new` and `open`, no app-store reviews and posts a summary to Discord that links back to each original ticket and highlights the ones worth looking into. For each ticket it assigns a category, infers severity, guesses a likely root cause, identifies platform and app version, groups likely duplicates into clusters, and ranks by priority.

| | |
| --- | --- |
| Runs | `session-ops@zendesk-digest.timer`, Mon–Fri 10:00 Australia/Melbourne: `zendesk-resolve-reviews --apply`, then `zendesk-triage` |
| Secrets | `/etc/session-ops/zendesk.env`: the Zendesk API token, the triage channel's webhook, and the Claude Code CLI login of the `zendesk` account |
| Dry run | `session-ops run zendesk-digest --dry-run`; `uv run zendesk-triage --window-hours 72 --dry-run` from a checkout |
| Re-run | `systemctl start session-ops@zendesk-digest.service` |
| Logs | `journalctl -u session-ops@zendesk-digest -n 50 --no-pager` |

## Categories

`CATEGORY_SPECS` in [triage.py](../../src/session_ops/zendesk/triage.py) is the single source of truth — the schema enum, the Discord labels and emoji, which categories count as urgent, and the prompt guidance are all derived from it, so adding a category is one edit.

| Category | Notes |
| --- | --- |
| `security_report` | Vulnerability or exploit disclosure |
| `legal_or_data_request` | GDPR, subpoena, law enforcement |
| `bug_report` | Something is broken |
| `account_access` | Lost recovery phrase, locked out |
| `policy_question` | Law/regulation questions ("Chat Control", encryption backdoors) |
| `low_star_review` | ≤3★ app-store review — these often hide a real bug |
| `positive_review` | 4-5★ review, no actionable content |
| `feature_request`, `question`, `spam_or_solicitation`, `other` | |
| `abuse_report` | One user reporting another for illegal content. ~11% of non-review tickets, and nothing anyone can act on |

The first two are **urgent categories**: they are not bugs, so the model rates their severity `not_applicable`. Marking by severity alone gave them the calmest marker and sorted them last, so category urgency wins — they lead their line with 🚨, sort ahead of everything else, and cannot be pushed out of the digest by the display cap.

`abuse_report` sits at the other end. **Session is metadata-free by design: there is no action available on a reported Session ID**, not for Session and not for the support team. At ~11% of non-review tickets they were crowding out the tickets that can actually be acted on, so they are the one category the digest collapses — a single 🔇 line at the very bottom carrying the count and the ticket links, emitted whatever the model flagged, never spending a highlight slot. The volume stays visible; the false alarm goes away.

## App-store reviews are not triaged

73% of tickets are AppFollow-imported app-store reviews. **The digest excludes them entirely** — the query carries `-via:any_channel`.

Not because they carry nothing: 1,022 of the unsolved ones are ≤3★ and many are bug reports in disguise. Because a review is not work a digest can queue up for someone. It takes **one** developer response, which replaces any previous one, and there is no way to ask a follow-up question — so "assign it to a human tomorrow" is not a thing you can do with it. The volume stays visible in the header's review count, and [Zendesk Resolve Positive Reviews](#zendesk-resolve-positive-reviews) still clears the 4-5★ ones.

Detection uses the Zendesk `via.channel`, which identified reviews with no false positives in a 3,662-ticket sample (2,656/2,656). **Not** tags — only 287 of those reviews carried the `app-store` tag.

The star-floor machinery (`partition_reviews`, `--review-star-floor`, `--include-positive-reviews`) is still in the code and still runs, but only bites when `--query`/`ZENDESK_QUERY` overrides the default and pulls reviews back in. On a normal run it sees none.

## What else is out of scope

- **`pending` tickets.** Somebody already replied and the ball is with the customer; the "Pending to Solved" automation resolves them after 72h. The query is `status<pending`, so `new` and `open` only.
- **Tickets only we touched.** The window is on `updated>`, and `updated_at` moves on any change — a tag edit, the hourly automation, and every private note the `claude:` commands write. So the run drops anything whose `requester_updated_at` falls outside the window. Measured on a real 72h window: 79 fetched, 23 the requester had actually touched.

## Content-free tickets

Twitter DM tickets arrive with `description` identical to `subject` — both just `"Conversation with <handle>"` — which is 15% of non-review tickets and unclassifiable as fetched. For those only, `hydrate_descriptions` fetches a page of up to 10 comments and joins every body that differs from the subject into the description; later replies often carry the actual detail. Hydration is an enrichment, so an HTTP error or an unreachable endpoint leaves the ticket as-is rather than failing the run (`--no-hydrate` to skip it entirely).

The script (`zendesk-triage`, [triage.py](../../src/session_ops/zendesk/triage.py)) fetches the tickets in a rolling time window, classifies the whole batch in one schema-enforced request through the Claude Code CLI, and posts a Discord digest: a short header, then one line per ticket worth looking into.

Each line leads with a severity marker, a category emoji and a platform icon, links the ticket id, and carries the model's one-line summary plus its root-cause guess:

```
🗂️ **Zendesk triage** — analyzed **16** of **46** tickets in the window (updated in the past 3 days). Skipped **30** positive app-store review(s).
Backlog: **428** awaiting a reply, excluding app-store reviews (**5,252** more are reviews, not triaged).
**9** worth looking into.
⭐ **6** · 🐛 **3** · ❓ **2** · 🔇 **2** · 🔑 **1** · ⚖️ **1** · 🔒 **1**
Likely duplicates: **push-notifications-not-delivered** ×5 (#27637, #27610, #27606, #27605)
🚨 | ⚖️ | ❔ | #27632 · Police summons demanding user details for a Session ID
🚨 | 🔒 | 🤖 | #27603 · Exported component lets another app obtain internal SharedPreferences | Likely cause: Improperly exported provider allowing external apps to trigger file sharing
🟠 | ⭐ | 🍎 | #27610 · Messages not delivered for days; nothing shows even after opening | Likely cause: Push notification delivery / message retrieval failure
🟠 | 🐛 | 🤖 | 🔄 #27605 · Message and call notifications only appear when the app is opened | Likely cause: Push notification service failure on Android
🔇 **2** abuse reports — reported Session IDs, nothing actionable (#27640, #27641)
```

| Column | Values |
| --- | --- |
| Severity | 🔥 crash · 💥 data loss · 🟠 major · 🟡 minor · ⚪ cosmetic · ▫️ not applicable — replaced by 🚨 on the urgent categories |
| Category | The emoji from `CATEGORY_SPECS`, so it matches the tally line |
| Platform | 🤖 Android · 🍎 iOS · 🖥️ desktop (all three) · 🌐 multiple · ❔ unknown |

The header accounts for the batch in full, so nothing is dropped silently. The backlog line is scoped exactly like the analysis — `status<pending`, **excluding app-store reviews** — so the number and the tickets under it mean the same thing. That matters because 92% of unsolved tickets are AppFollow reviews, so the unqualified number reads as roughly 13× the queue that actually needs a human (5,680 against 428). Both counts come from Zendesk's count-only search endpoint, one request each and both best-effort — if the review-excluded count fails, the line falls back to the plain total rather than disappearing. The category tally counts abuse reports like anything else, so the numbers still sum to what was analyzed; the collapsed line at the bottom is where they are listed, and its links stop at a character budget (the remainder counted as `+N more`) so a heavy day cannot push a message past 2,000.

**One block per ticket, and the digest is read-only.** Each line is its own Components V2 Text Display inside one Container, so the digest is skimmed rather than read as a wall. Nothing in it is interactive: replies are written on the ticket itself (see [Zendesk Reply from a Private Note](zendesk-relay.md)), and a button here would open a compose flow that no longer exists.

Two limits bound a message and whichever binds first splits it — 40 components, which no longer binds now that a ticket costs one, and `MAX_MESSAGE_TEXT_CHARS` across all its text, which does. `MAX_ENTRIES_PER_MESSAGE` stays at 10 because that is a readable message, not because it is the ceiling. Lines are clipped (`SUMMARY_CHARS`, `ROOT_CAUSE_CHARS`), and each message records which ticket ids it accounts for, which is what makes a partial post failure recoverable.

**That is what lets everything here go over one incoming webhook** — the digest, the positive-review tally and the failure alerts alike. A webhook no application owns may send non-interactive components and nothing else, so adding an interactive one means moving the digest back to a bot token and a channel id. The request needs `?with_components=true` (`digest_webhook_url`): Discord ignores the `components` field on a webhook post without it, and the digest is nothing but components.

## Deduplication

The window is 72h against runs a day apart, so consecutive runs overlap. A state file (`--state`) records each reported ticket's `requester_updated_at`, giving three outcomes per ticket:

| Ticket | Outcome |
| ------ | ------- |
| Not seen before | Analyzed and reported |
| Seen, requester hasn't been back | **Skipped before the model call** — costs no tokens |
| Seen, requester added something | Re-analyzed, reported, and flagged 🔄 on its line |

State is written only on a real run, and only for tickets covered by messages Discord **accepted**. Each message carries the ticket ids it accounts for, so a partial failure records exactly what landed: already-posted messages aren't repeated next run, and undelivered tickets stay eligible. The run then exits non-zero. Neither `--dry-run` nor `--no-discord` writes state — nothing was delivered, so every ticket stays eligible for the next run.

Two caveats worth knowing:

- The comparison is on `requester_updated_at`, from the ticket's metric set, **not** `updated_at`. `updated_at` moves on any change — our own replies, a tag edit, and in this account an hourly automation that bumps tickets at :01 past the hour — so deduping on it re-reports the same ticket every run. Measured on a real window: an automation pass over 18 tickets produced 18 re-reports under `updated_at` and 0 under `requester_updated_at`. The metric sets are sideloaded through `show_many`, one request per 100 tickets, and a failed sideload falls back to `updated_at` — noisy, never silent.
- Unchanged tickets are filtered out *before* the model call, which is what makes the dedup free. The trade-off is that duplicate-cluster detection only sees the new and changed tickets in a given run, not the whole window.

> **Note:** This repo is public, so ticket content is never written to the run logs or the job summary — ticket detail goes only to the Discord webhook (a private channel), and the links require Zendesk auth to open. The one exception is the local `--dump-batch` debugging flag, which writes ticket content to a file you name; `*.json` at the checkout root is gitignored to keep those out of the repo.

## Required Secrets

| Secret                | Description                                             |
| --------------------- | ------------------------------------------------------- |
| `ZENDESK_SUBDOMAIN`   | Zendesk subdomain (`mycompany` → `mycompany.zendesk.com`) |
| `ZENDESK_EMAIL`       | Agent email used for Zendesk API-token auth             |
| `ZENDESK_API_TOKEN`   | Zendesk API token                                       |
| `ZENDESK_DISCORD_WEBHOOK_URL` | Discord incoming webhook for the triage channel, shared with the tally and the failure alerts |

## Claude Authentication

There is no Claude key. Both Claude calls — the digest's classification and the reply flow's translation — shell out to the locally installed Claude Code CLI (`claude --print`), which authenticates as whoever it is logged in as. On the host that is the service user; see [deploy/README.md](../../deploy/README.md).

The trade is process startup, a few seconds per call, against holding an API credential on the box. That is invisible on a nightly digest, and on the reply dialog Discord keeps the interaction open while it runs.

## Optional Configuration

| Setting                | Where            | Default                                                 | Description |
| ---------------------- | ---------------- | ------------------------------------------------------- | ----------- |
| `--window-hours`       | flag             | *(unset)*                                               | Analyze tickets the requester touched in the last N hours. There is no parser default: absent, the run uses `DEFAULT_QUERY` and no window at all. The `72` the digest runs with is passed by [`digest_job.py`](../../src/session_ops/zendesk/digest_job.py) |
| `--state`              | flag             | *(unset)*                                               | Dedup state file. [`jobs.toml`](../../src/session_ops/jobs.toml) points this at `/var/lib/session-ops/zendesk-digest/seen.json` |
| `--state-retention-days` | flag           | `30`                                                    | Forget state entries older than N days |
| `ZENDESK_QUERY`        | env / `--query`  | *(unset)*                                               | Explicit Zendesk search query. Overrides `--window-hours` entirely |
| `ZENDESK_TRIAGE_MODEL` | env / `--model`  | `claude-opus-5`                                         | Overrides the model. Takes a full id, or a shorthand (`opus`, `sonnet`, `haiku`) mapped to an id via `API_MODEL_ALIASES`. **Leave it unset for normal operation** — the default lives in the script so there's one place to change it |
| `--findings`           | flag             | *(unset)*                                               | Render a findings JSON classified elsewhere, skipping Zendesk and Claude entirely. Pairs with `--dump-batch` |
| `--max-tickets`        | flag             | `100`                                                   | Runaway guard on tickets analyzed per run, **not** a batch size. Zendesk's search API caps a query at 1000 results, so higher values don't fetch more |
| `--batch-size`         | flag             | `400`                                                   | Split batches larger than this across multiple requests |
| `--review-star-floor`  | flag             | `3`                                                     | Classify app-store reviews at or below N stars; count the rest. Only reachable via an explicit `--query` — the default excludes reviews |
| `--include-positive-reviews` | flag       | off                                                     | Classify every review, including 4-5★ ones. Same caveat |
| `--no-hydrate`         | flag             | off                                                     | Skip fetching comments for content-free tickets |
| `--no-discord`         | flag             | off                                                     | Analyze but post nothing, printing counts only. Records no state, so the next run still reports those tickets. Unlike `--dry-run` it prints no ticket content |
| `--effort`             | flag             | `medium`                                                | Claude reasoning effort (`low`–`max`) |

### Why this model, and why pinned

**Opus**, because the hard part of this job isn't per-ticket classification — enum-constrained categories with prompt guidance is squarely mid-tier work. It's the two batch-wide fields: `cluster` has to spot that a German app-store review and an English bug report describe one root cause, and `priority_rank` has to stay consistent across the whole batch. Those need the model to hold ~45 heterogeneous tickets in mind at once. The exact-transcription requirement (a 66-character Session ID copied verbatim) points the same way. And the entire job costs **single-digit dollars a month** on any current model — roughly $10 on Opus 5 against $6 on Sonnet 5 and $2 on Haiku 4.5 — so trading classification quality for a few dollars would be optimising the wrong thing when the cost of a miss is an unseen security report or a crash cluster nobody grouped.

**Pinned to an id rather than the `opus` alias**, because this is an unattended digest. An alias resolves to the newest Opus the credential allows, so severity calibration and cluster labels would shift on someone else's release schedule, with no run in between to notice it. Bumping the pin is a deliberate one-line change in [triage.py](../../src/session_ops/zendesk/triage.py) (`DEFAULT_MODEL`).

Two cases for overriding it:

- **Large backfills.** A `reset_state` run at `--max-tickets 1000` chunks into 400-ticket requests, where Opus latency and spend actually show up and cross-chunk cluster fidelity is already reduced by design. `ZENDESK_TRIAGE_MODEL=sonnet` for those.
- **Never Fable 5.** It prices above Opus tier, targets long-horizon agentic reasoning, and requires 30-day data retention — all wrong for batch classification of support tickets.

### Batch size vs. ticket cap

These do different jobs, and conflating them is how you get a silently truncated digest:

- **`--max-tickets`** bounds how much of the Zendesk result set is fetched. It never binds on a 72h window (~70 tickets); it exists so a spam flood or a wide backfill can't run away. 1000 is also [Zendesk's own search result limit](https://developer.zendesk.com/api-reference/ticketing/ticket-management/search/#results-limit) — the API returns `422` for any page past it, so the fetch stops at 1000 regardless of what you pass, and reports the matched-vs-analyzed gap rather than failing.
- **`--batch-size`** bounds how many tickets go into a *single* model request. Anything larger is split across requests and the findings are concatenated.

The split is necessary because output tokens, not context, are the binding constraint. Measured on real tickets: **~118 input tokens and ~102 output tokens per ticket**, with adaptive thinking drawing from the same output budget.

| Batch | Input | Output needed | Fits in one request? |
| ----- | ----- | ------------- | -------------------- |
| 45 (typical daily) | ~5K | ~5K | Yes |
| 400 (`--batch-size`) | ~47K | ~41K | Yes, with room for thinking |
| 1000 (`--max-tickets`) | ~118K | ~102K | **No** — leaves only ~26K of the 128K output ceiling for thinking |

If a single request ever does hit the ceiling, the JSON never closes and no `structured_output` comes back — the script exits naming that and the `--batch-size` to lower, rather than rendering a digest that is silently short.

> Chunking is per-request, so `cluster` labels and `priority_rank` are only meaningful within a chunk. Batches large enough to split are ones where completing at all matters more than cross-chunk cluster fidelity.

## Schedule

Runs **Monday to Friday at 10:00 Melbourne** over a 72h window (~70 tickets) — 00:00 UTC in winter, 23:00 UTC the previous day under AEDT. The cron this replaces had to pin UTC+10 year-round and drift an hour against local time, because GitHub cron is UTC-only; `OnCalendar=` takes a named zone, which tracks daylight saving and keeps the day-of-week local as well. The timezone belongs inside the expression; there is no `Timezone=` key in a `[Timer]` and systemd ignores one silently, so check any change with `systemd-analyze calendar`. Unlike the cron, a host that was asleep at 10:00 still gets its digest once on the next boot (`Persistent=yes`).

The window is on `updated>`, not `created>`, so a ticket the requester adds detail to days after opening it is fetched again — a created-window would never see it. 72h rather than the 24h between runs so a failed run doesn't drop a day and Monday still reaches back past the weekend. Neither the overlap nor the wider net duplicates posts, because of the dedup state above.

[Zendesk Resolve Positive Reviews](#zendesk-resolve-positive-reviews) runs first, in [`digest_job.py`](../../src/session_ops/zendesk/digest_job.py). Order matters: the triage query is `status<pending`, so a review the resolver solves leaves the window — running second would re-count reviews just closed. Its failure does not stop the digest, because the resolver is an optimisation for it rather than a precondition; the failure is still reported, so a resolver broken for weeks cannot pass for one with nothing to do.

Run it by hand with `sudo systemctl start session-ops@zendesk-digest.service`, which does exactly what the timer does. For anything narrower, invoke the scripts directly — `--window-hours`, `--max-tickets`, `--query`, and `--no-discord` to exercise the job without posting (that run records nothing, so the next one still reports the tickets it saw). Failures are reported by the run, and by `OnFailure=` for whatever the run could not report; see [session-ops-silence](session-ops-silence.md).

### How state survives between runs

State is kept in a plain file under `/var/lib/session-ops/zendesk-digest`. Losing it re-reports the window once — noisy, never wrong — so it needs persisting, not backing up. It is not committed: this repo is public, and ticket ids plus timestamps would leak ticket volume and activity rates.

The file is written atomically (`os.replace`) so a crash mid-write cannot corrupt it, and it is pruned to `--state-retention-days`. A missing, corrupt, or wrong-shaped file degrades to "treat every ticket as new" rather than failing — noisy for one run, never wrong.

`Type=oneshot` on the unit and a single timer mean two runs cannot overlap, so nothing races on the file.

## Tests

```
uv run python -m unittest discover -s tests/zendesk -t . -v
```

The `dev` dependency group carries what only the tests need: `test_relay.py` drives the
relay through starlette's `TestClient`, which needs an HTTP client the deployment does not.

Offline tests covering the window arithmetic, dedup partitioning, state round-trip and pruning, corrupt-state degradation, Discord card rendering and message chunking, defensive JSON parsing, and the retry/pagination behaviour with a stub session. No secrets or network access needed.

## Local Testing

Local runs need the `claude` CLI on `PATH` and logged in (`claude --version`), alongside the Zendesk credentials. `--dry-run` prints the Discord payload instead of posting, so no webhook is needed. Keep it to local runs: it prints ticket content. `--no-discord` prints counts only:

```
uv sync
export ZENDESK_SUBDOMAIN=... ZENDESK_EMAIL=... ZENDESK_API_TOKEN=...

# what the unit runs, minus the Discord post and the state file
uv run zendesk-triage --window-hours 72 --dry-run

# keep it cheap while iterating on the rendering
uv run zendesk-triage --window-hours 12 --max-tickets 5 --dry-run

# same run without the payload dump: fetches, classifies, posts nothing
uv run zendesk-triage --window-hours 72 --no-discord

# or take the model out of the loop: dump the batch, classify it by hand,
# and feed the findings back in to render
uv run zendesk-triage --dump-batch /tmp/batch.json --window-hours 48
uv run zendesk-triage --findings /tmp/findings.json --dry-run
```

## Zendesk Resolve Positive Reviews

The triage's opening act: it solves the 4-5★ AppFollow reviews that were never going to be actioned, so the unsolved backlog reflects work that actually exists. When this was written **5,253** reviews were unsolved — **4,812** of them still `new` — against **428** non-review unsolved tickets. Solving reviews was already being done by hand: **4,959** were already solved or closed. The job has since solved **3,882**, and the reviews it now finds are `open` rather than `new` — see the status bullet below.

> ⚠️ **This writes to Zendesk.** The scheduled run always applies. Run by hand it is a **dry run** unless you pass `--apply`, so nothing can bulk-edit tickets by accident. Read the warning at the top of [resolve_reviews.py](../../src/session_ops/zendesk/resolve_reviews.py) before the first applied run.

### What it will and will not touch

Deliberately narrow, because a mis-aimed bulk status change is not recoverable by re-running:

- **App-store reviews only**, by the same detection the triage uses — `api.is_store_review`, so the two can't drift apart. Every fetched ticket is re-checked locally, since the query can't express the rating.
- **Rated 4★ or better.** A fixed floor (`MIN_STARS`), not a flag — 3★ and below are what the triage reads as bug reports in disguise, so a lower floor would have this job close the reviews most worth looking at. A review whose stars can't be parsed from the subject is skipped, never solved.
- **`new` or `open`** (`status<pending`). The "Auto Assign to Support" automation fires an hour after a review arrives and gives it a group, which moves it to `open` — so neither the status nor the assignee marks a review a human has handled, and all 628 open 4-5★ reviews share one assignee and one group. `pending` and `hold` are empty on this channel, which makes them where an agent replying to a review puts it, and the bound that keeps this job off it. There is deliberately no flag to widen this further.
- **`solved`, never `closed`.** Closed is irreversible. Solved is reversible, but only for about four days: the account's *Close ticket 4 days after status is set to solved* automation takes it from there, so a batch can be reviewed and reopened inside that window and not after it.
- **Tagged** `auto-resolved-review`, so they stay identifiable and a trigger can exclude them, and annotated with a **private** note — a public comment would email the person who wrote the review.

That tag is also how a run is reviewed afterwards. An applied run prints — and posts — an agent-search link to what it just solved, so the set can be eyeballed, or found again and reopened, without reconstructing the query by hand:

```
Solved 7 of 7 ticket(s).
  review what changed: https://acme.zendesk.com/agent/search/1?type=ticket&q=tags%3Aauto-resolved-review%20status%3Asolved%20updated%3E2026-08-16
```

The date bound is yesterday rather than today because Zendesk's date search is day-granular and `updated>` is exclusive — today's date would filter out the very tickets the run just solved — and the spare day absorbs the account timezone the search interprets dates in. Since the job runs once a day at most, that window is this run and, at worst, yesterday's. A run that solved nothing links the tag without a date bound instead, so the link shows the job's history rather than landing on an empty search.

### Before the first applied run

Solving a ticket fires triggers and automations, and an AppFollow requester may carry a real email address. **A satisfaction survey trigger would email thousands of app-store reviewers.** Check Admin Center → Objects and rules → Business rules first, then do the first applied run with `--max-tickets 5` so the effects are observable before they're bulk.

### How it drains

No state file: a solved ticket drops out of the query, so runs are idempotent. Zendesk's search API caps at 1,000 results, so a run can never see more than that — the first few runs work the backlog down and after that five runs a week comfortably clear the ~420 reviews a week that arrive. `update_many` takes [100 ids per request](https://developer.zendesk.com/api-reference/ticketing/tickets/tickets/#update-many-tickets) and is asynchronous, so each batch's job is polled to completion and per-ticket failures fail the run rather than being reported as success.

### What it posts

Every applied run reports to the same Discord channel as the triage, so a job that bulk-edits tickets is visible where those tickets are already discussed:

> ✅ Marked **12** 4★ and **31** 5★ app-store reviews as solved in Zendesk.
> 🔍 [Review what changed](#)
> 📥 **4,769** more tickets match than this run looked at; the next run picks them up.

The rating split is the point — a bare total wouldn't say which reviews went. The tally counts the ids each bulk job **confirmed**, not the ids submitted, so the number is what Zendesk actually changed; a batch with per-ticket failures adds a line saying so, next to the count it contradicts. The leftover line appears only while there's a backlog left to drain.

**A run that solved nothing reports that too**, rather than staying quiet:

> 💤 No 4★ or better app-store reviews left to solve — looked at **48** untouched tickets.
> 🔍 [Everything this job has solved](#)

Silence would be indistinguishable from a job that has quietly stopped working — a broken query, a rotated token, a schedule that no longer fires — and this job exists to keep a number moving that nobody watches directly, so "looked, found nothing" is the half worth hearing. The count of what it examined is what separates the two. Eligible reviews that all *failed* get their own wording (`None of the 3 eligible app-store reviews were solved`), because reporting that as a quiet day would dress a broken run up as a clean one.

**A run that died reports too**, from the job runner rather than the script — a Zendesk `4xx`, a bulk job that never completes, a host that rebooted all exit before a message exists:

> ❌ **zendesk-digest** on `angus`: 1 of 2 targets failed.
> ⚠️ **resolve reviews**: Zendesk returned 403 on the bulk update.
> ✅ **digest**
> `journalctl -u session-ops@zendesk-digest -n 50 --no-pager` · re-run: `systemctl start session-ops@zendesk-digest.service`

It says nothing about counts, because it also fires after the script has already posted a tally alongside per-ticket failures, and nothing about the cause, because the run may have died before it had one — it points at the journal instead of guessing.

A dry run prints the message it would have posted instead of posting it, and `--no-discord` solves without reporting. The message is a tally rather than a per-ticket list, so unlike the triage digest it can't spill into a second message.

### Required Secrets

`ZENDESK_SUBDOMAIN`, `ZENDESK_EMAIL`, `ZENDESK_API_TOKEN` — the same three the triage uses — plus `ZENDESK_DISCORD_WEBHOOK_URL`, the triage channel's own webhook, so the tally lands next to the digests it accounts for. Not the shared `DISCORD_WEBHOOK_URL`: a webhook is bound to the channel it was created in. No Claude credentials: it classifies nothing.

The webhook is resolved before the run fetches anything, so a missing secret stops it rather than letting it bulk-edit tickets it then can't report; a dry run doesn't need one.

### Schedule

No timer of its own: it is the first target of [`digest_job.py`](../../src/session_ops/zendesk/digest_job.py), so it runs immediately before the digest, Monday to Friday, and applies. See the digest's Schedule section for why it must go first. Rehearse it by hand without `--apply` for a dry run, and bound a first real one with `--max-tickets`.

It is an optional target: its failure is reported to the triage channel and the digest proceeds — resolving is an optimisation for the digest, not a precondition. The run still succeeds, so the silence checker keeps seeing a digest that posts, while the alert keeps a resolver broken for weeks from looking like one with nothing to do.
