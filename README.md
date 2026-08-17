# Session Shared Scripts

This repo houses scripts which are shared between the different platform repos for Session, it also contains a number of Actions used to automatically sync some shared elements across the repos.

## Crowdin Translation Workflow

Automated workflow that downloads translations from Crowdin, validates them, and creates PRs for iOS and Android platforms and for the Typescript Localization Module for Desktop and QA.

### Required Secrets

| Secret              | Description                                             |
| ------------------- | ------------------------------------------------------- |
| `CROWDIN_API_TOKEN` | Crowdin personal access token (see scopes below)         |
| `CROWDIN_PR_TOKEN`  | GitHub token with PR creation permissions                |

#### Crowdin token scopes

Crowdin scopes personal access tokens per endpoint family, so a token missing one
scope returns `403 Forbidden` on just those endpoints while every other call keeps
working. The scripts in this repo need:

| Scope                  | Value                | Needed for                                                              |
| ---------------------- | -------------------- | ----------------------------------------------------------------------- |
| Projects               | `project`            | Project details and the target-language list                            |
| Source files & strings | `project.source`     | Listing source strings (`approve_strings.py`, multiple-translations report) |
| Translations           | `project.translation` | Translation exports, plus reading/adding approvals and translations     |
| Glossaries             | `glossary`           | Non-translatable strings (glossary terms)                               |

> **Note:** Scopes only cap what a token may do — they don't grant anything the
> token's Crowdin account can't already do, so the account also needs a project
> role that allows it (manager/proofreader for anything that writes, e.g. the
> approvals `POST` in `approve_strings.py`).

### Workflow Inputs

| Input                    | Default | Description                              |
| ------------------------ | ------- | ---------------------------------------- |
| `UPDATE_PULL_REQUESTS`   | `true`  | Create/update PRs for all platforms      |
| `SKIP_VALIDATION_ERRORS` | `false` | Continue even if string validation fails |

### Schedule

Runs automatically every Monday at 00:00 UTC.

### Validation Rules

#### All Strings (including plurals)

- **Valid `{variable}` syntax** - No broken braces (`{`, `}`, `{}`, `{ space }`)
- **Allowed HTML tags only** - Only `<b>`, `<br/>`, `<span>`
- **Valid tag syntax** - No malformed `<` (e.g., `<script>`, `<123>`)

#### Non-Plural Strings Only

- **Variables match English** - Same `{variables}` as source locale
- **Tag count matches English** - Same number of tags (warning only)

#### All Locales

- **No extra keys** - No strings that don't exist in English

> **Note:** Plural strings skip variable/tag comparison because languages have different plural forms (English: 2, Arabic: 6, Russian: 4). It would be nice to add suppot for plural validation in the future.

## Zendesk Ticket Triage

Claude reviews recently-created unsolved Zendesk tickets fetched from the Zendesk API and posts a summary to Discord that links back to each original ticket and highlights the ones worth looking into. For each ticket it assigns a category, infers severity, guesses a likely root cause, identifies platform and app version, groups likely duplicates into clusters, and ranks by priority.

### Categories

`CATEGORY_SPECS` in [triage.py](zendesk_triage/triage.py) is the single source of truth — the schema enum, the Discord labels and emoji, which categories count as urgent, and the prompt guidance are all derived from it, so adding a category is one edit.

| Category | Notes |
| --- | --- |
| `abuse_report` | One user reporting another for illegal content. ~11% of non-review tickets |
| `security_report` | Vulnerability or exploit disclosure |
| `legal_or_data_request` | GDPR, subpoena, law enforcement |
| `bug_report` | Something is broken |
| `account_access` | Lost recovery phrase, locked out |
| `policy_question` | Law/regulation questions ("Chat Control", encryption backdoors) |
| `low_star_review` | ≤3★ app-store review — these often hide a real bug |
| `positive_review` | 4-5★ review, no actionable content |
| `feature_request`, `question`, `spam_or_solicitation`, `other` | |

The first three are **urgent categories**: they are not bugs, so the model rates their severity `not_applicable`. Marking by severity alone gave them the calmest marker and sorted them last, so category urgency wins — they lead their line with 🚨, sort ahead of everything else, and cannot be pushed out of the digest by the display cap.

### App-store review filtering

73% of tickets are AppFollow-imported app-store reviews, and 71% of those are 5★ — 59% of *all* tickets are 4-5★ reviews that are never actionable. Those are counted, not classified, cutting the batch roughly 60% (a real run: 48 fetched → 20 classified).

Detection uses the Zendesk `via.channel`, which identified reviews with no false positives in a 3,662-ticket sample (2,656/2,656). **Not** tags — only 287 of those reviews carried the `app-store` tag. Reviews whose star rating can't be parsed are kept rather than dropped. Use `--include-positive-reviews` to disable, or `--review-star-floor` to move the threshold.

### Content-free tickets

Twitter DM tickets arrive with `description` identical to `subject` — both just `"Conversation with <handle>"` — which is 15% of non-review tickets and unclassifiable as fetched. For those only, `hydrate_descriptions` fetches a page of up to 10 comments and joins every body that differs from the subject into the description; later replies often carry the actual detail. Hydration is an enrichment, so an HTTP error or an unreachable endpoint leaves the ticket as-is rather than failing the run (`--no-hydrate` to skip it entirely).

The script (`zendesk_triage/triage.py`) fetches the tickets in a rolling time window, classifies the whole batch in one schema-enforced request to the Anthropic API, and posts a Discord digest: a short header, then one line per ticket worth looking into.

Each line leads with a severity marker, a category emoji and a platform icon, links the ticket id, and carries the model's one-line summary plus its root-cause guess:

```
🗂️ **Zendesk triage** — analyzed **16** of **46** tickets in the window (created in the past 2 days). Skipped **30** positive app-store review(s).
Backlog: **428** unsolved excluding app-store reviews (**5,252** more are reviews, not triaged).
**9** worth looking into.
⭐ **6** · 🐛 **3** · ❓ **2** · 🔑 **1** · ⚖️ **1** · 🔒 **1**
Likely duplicates: **push-notifications-not-delivered** ×5 (#27637, #27610, #27606, #27605)
🚨 | ⚖️ | ❔ | #27632 · Police summons demanding user details for a Session ID
🚨 | 🔒 | 🤖 | #27603 · Exported component lets another app obtain internal SharedPreferences | Likely cause: Improperly exported provider allowing external apps to trigger file sharing
🟠 | ⭐ | 🍎 | #27610 · Messages not delivered for days; nothing shows even after opening | Likely cause: Push notification delivery / message retrieval failure
🟠 | 🐛 | 🤖 | 🔄 #27605 · Message and call notifications only appear when the app is opened | Likely cause: Push notification service failure on Android
```

| Column | Values |
| --- | --- |
| Severity | 🔥 crash · 💥 data loss · 🟠 major · 🟡 minor · ⚪ cosmetic · ▫️ not applicable — replaced by 🚨 on the urgent categories |
| Category | The emoji from `CATEGORY_SPECS`, so it matches the tally line |
| Platform | 🤖 Android · 🍎 iOS · 🖥️ desktop (all three) · 🌐 multiple · ❔ unknown |

The header accounts for the batch in full, so nothing is dropped silently. The backlog line deliberately **excludes app-store reviews**: 92% of unsolved tickets are AppFollow reviews, so the unqualified number reads as roughly 13× the queue that actually needs a human (5,680 against 428). Both counts come from Zendesk's count-only search endpoint, one request each and both best-effort — if the review-excluded count fails, the line falls back to the plain total rather than disappearing. An abuse report also carries the reported Session ID on its line, since that is the actionable part and it saves opening the ticket.

**Plain message content, no embeds.** The lines carry their own structure, so an embed added a border and nothing else. The cost is the character budget: Discord caps message content at 2,000 against an embed description's 4,096, and a masked link on the id spends 54 characters that the reader never sees. A real 9-highlight day comes to ~2,400 characters, so it arrives as two messages. Lines are clipped (`SUMMARY_CHARS`, `ROOT_CAUSE_CHARS`) and chunked against 2,000, counting the newlines that join them; each message records which ticket ids it accounts for, which is what makes a partial post failure recoverable.

### Deduplication

The daily window is 48h, so consecutive runs overlap. A state file (`--state`) records each reported ticket's Zendesk `updated_at`, giving three outcomes per ticket:

| Ticket | Outcome |
| ------ | ------- |
| Not seen before | Analyzed and reported |
| Seen, `updated_at` unchanged | **Skipped before the model call** — costs no tokens |
| Seen, `updated_at` moved | Re-analyzed, reported, and flagged 🔄 on its line |

State is written only on a real run, and only for tickets covered by messages Discord **accepted**. Each message carries the ticket ids it accounts for, so a partial failure records exactly what landed: already-posted messages aren't repeated next run, and undelivered tickets stay eligible. The run then exits non-zero. `--dry-run` never writes state.

Two caveats worth knowing:

- **Any** agent action bumps `updated_at` (a reply, a tag, a status change), not just an end-user comment, so agent activity can trigger a re-report. Narrowing this to new end-user comments would need per-ticket comment fetches.
- Unchanged tickets are filtered out *before* the model call, which is what makes the dedup free. The trade-off is that duplicate-cluster detection only sees the new and changed tickets in a given run, not the whole window.

> **Note:** This repo is public, so ticket content is never written to the run logs or the job summary — ticket detail goes only to the Discord webhook (a private channel), and the links require Zendesk auth to open. The one exception is the local `--dump-batch` debugging flag, which writes ticket content to a file you name; `zendesk_triage/*.json` is gitignored to keep those out of the repo.

### Required Secrets

| Secret                | Description                                             |
| --------------------- | ------------------------------------------------------- |
| `ZENDESK_SUBDOMAIN`   | Zendesk subdomain (`mycompany` → `mycompany.zendesk.com`) |
| `ZENDESK_EMAIL`       | Agent email used for Zendesk API-token auth             |
| `ZENDESK_API_TOKEN`   | Zendesk API token                                       |
| `ANTHROPIC_API_KEY`   | Claude API key — see [Claude authentication](#claude-authentication) |
| `ZENDESK_DISCORD_WEBHOOK_URL` | Discord webhook for the triage channel. Deliberately its own secret, not the `DISCORD_WEBHOOK_URL` the failure notifier and Crowdin report share — a Discord webhook is bound to the channel it was created in, so pointing triage elsewhere means a separate webhook. Note that triage *failures* still go to `DISCORD_WEBHOOK_URL` via the failure-notification workflow |

### Claude Authentication

Classification goes through the Anthropic API with an `ANTHROPIC_API_KEY`, in CI and locally alike — an organization-owned credential that doesn't draw on any individual's subscription quota.

If you go looking for that key and can't find one: an API key only exists inside a **Claude Console organization** (`platform.claude.com`), which is a separate organization from a claude.ai Pro/Max/Team/Enterprise subscription, with its own membership and billing. A claude.ai admin console has no API keys in it at all, so the usual answer is that no Console organization exists yet rather than that you're missing a permission.

### Optional Configuration

| Setting                | Where            | Default                                                 | Description |
| ---------------------- | ---------------- | ------------------------------------------------------- | ----------- |
| `--window-hours`       | workflow input / flag | `48`                                               | Analyze unsolved tickets created in the last N hours |
| `--state`              | flag             | *(unset)*                                               | Dedup state file. The workflow points this at the cached `.triage-state/seen.json` |
| `--state-retention-days` | flag           | `30`                                                    | Forget state entries older than N days |
| `ZENDESK_QUERY`        | env / `--query`  | *(unset)*                                               | Explicit Zendesk search query. Overrides `--window-hours` entirely |
| `ZENDESK_TRIAGE_MODEL` | repo variable / `--model` | `claude-opus-5`                                         | Overrides the model. Takes a full id, or a shorthand (`opus`, `sonnet`, `haiku`) mapped to an id via `API_MODEL_ALIASES`. **Leave it unset for normal operation** — the default lives in the script so there's one place to change it |
| `--findings`           | flag             | *(unset)*                                               | Render a findings JSON classified elsewhere, skipping Zendesk and Claude entirely. Pairs with `--dump-batch` |
| `--max-tickets`        | workflow input / flag | `1000` (workflow) / `100` (flag)                   | Runaway guard on tickets analyzed per run, **not** a batch size. The workflow passes `1000`; a bare `python triage.py` uses the script's own `DEFAULT_MAX_TICKETS` of `100`. Zendesk's search API caps a query at 1000 results, so higher values don't fetch more |
| `--batch-size`         | flag             | `400`                                                   | Split batches larger than this across multiple requests |
| `--review-star-floor`  | flag             | `3`                                                     | Classify app-store reviews at or below N stars; count the rest |
| `--include-positive-reviews` | flag       | off                                                     | Classify every review, including 4-5★ ones |
| `--no-hydrate`         | flag             | off                                                     | Skip fetching comments for content-free tickets |
| `--effort`             | flag             | `medium`                                                | Claude reasoning effort (`low`–`max`) |

#### Why this model, and why pinned

**Opus**, because the hard part of this job isn't per-ticket classification — enum-constrained categories with prompt guidance is squarely mid-tier work. It's the two batch-wide fields: `cluster` has to spot that a German app-store review and an English bug report describe one root cause, and `priority_rank` has to stay consistent across the whole batch. Those need the model to hold ~45 heterogeneous tickets in mind at once. The exact-transcription requirement (a 66-character Session ID copied verbatim) points the same way. And the entire job costs **single-digit dollars a month** on any current model — roughly $10 on Opus 5 against $6 on Sonnet 5 and $2 on Haiku 4.5 — so trading classification quality for a few dollars would be optimising the wrong thing when the cost of a miss is an unseen abuse report.

**Pinned to an id rather than the `opus` alias**, because this is an unattended digest. An alias resolves to the newest Opus the credential allows, so severity calibration and cluster labels would shift on someone else's release schedule, with no run in between to notice it. Bumping the pin is a deliberate one-line change in [triage.py](zendesk_triage/triage.py) (`DEFAULT_MODEL`).

Two cases for overriding it:

- **Large backfills.** A `reset_state` run at `--max-tickets 1000` chunks into 400-ticket requests, where Opus latency and spend actually show up and cross-chunk cluster fidelity is already reduced by design. `ZENDESK_TRIAGE_MODEL=sonnet` for those.
- **Never Fable 5.** It prices above Opus tier, targets long-horizon agentic reasoning, and requires 30-day data retention — all wrong for batch classification of support tickets.

#### Batch size vs. ticket cap

These do different jobs, and conflating them is how you get a silently truncated digest:

- **`--max-tickets`** bounds how much of the Zendesk result set is fetched. At the workflow's 1000 it never binds on a 48h window (~45 tickets); it exists so a spam flood or a wide `reset_state` backfill can't run away. 1000 is also [Zendesk's own search result limit](https://developer.zendesk.com/api-reference/ticketing/ticket-management/search/#results-limit) — the API returns `422` for any page past it, so the fetch stops at 1000 regardless of what you pass, and reports the matched-vs-analyzed gap rather than failing.
- **`--batch-size`** bounds how many tickets go into a *single* model request. Anything larger is split across requests and the findings are concatenated.

The split is necessary because output tokens, not context, are the binding constraint. Measured on real tickets: **~118 input tokens and ~102 output tokens per ticket**, with adaptive thinking drawing from the same output budget.

| Batch | Input | Output needed | Fits in one request? |
| ----- | ----- | ------------- | -------------------- |
| 45 (typical daily) | ~5K | ~5K | Yes |
| 400 (`--batch-size`) | ~47K | ~41K | Yes, with room for thinking |
| 1000 (`--max-tickets`) | ~118K | ~102K | **No** — leaves only ~26K of the 128K output ceiling for thinking |

If a single request ever does hit the ceiling, the JSON never closes and no `structured_output` comes back — the script exits naming that and the `--batch-size` to lower, rather than rendering a digest that is silently short.

> Chunking is per-request, so `cluster` labels and `priority_rank` are only meaningful within a chunk. Batches large enough to split are ones where completing at all matters more than cross-chunk cluster fidelity.

### Schedule

Runs daily at 07:00 UTC over a 48h window (~45 tickets). The window is 48h rather than 24h so a failed run doesn't silently drop a day of tickets; the resulting overlap doesn't produce duplicate posts because of the dedup state described above.

Triggerable manually via **workflow_dispatch** (optional `query` / `window_hours` / `max_tickets` inputs, plus `reset_state` to re-report the whole window). Failures are reported through the Discord failure-notification workflow, which watches this workflow by name — so renaming `Zendesk Ticket Triage` means updating the `workflows:` list in [`notify_failure.yml`](.github/workflows/notify_failure.yml) too.

#### How state survives between runs

State is kept in the **GitHub Actions cache**, not committed — this repo is public, and ticket IDs plus timestamps would leak ticket volume and activity rates. The workflow writes a unique cache key per run attempt and restores the most recent one by prefix:

```yaml
key: zendesk-triage-state-${{ github.run_id }}-${{ github.run_attempt }}
restore-keys: |
  zendesk-triage-state-
```

`run_attempt` is in the key because cache entries are **immutable**: a re-run reuses `run_id`, so keying on that alone would make the second attempt's save collide with the first's and write nothing. With it, attempt 2 saves its own entry and restores attempt 1's through the prefix — so tickets the first attempt already delivered aren't reposted.

The cache is **best-effort**, and the script is written to tolerate that — a missing, corrupt, or wrong-shaped state file degrades to "treat every ticket as new", which is noisy for one run but never wrong. Things that can lose state:

- **7 days without a cache hit** evicts the entry. The daily run keeps it warm, so this only bites if the workflow is disabled for a week.
- **Repo cache eviction** under the 10GB limit (LRU). The state file is a few KB, so this is unlikely.
- **Branch scoping:** caches written on the default branch are readable everywhere; a run on a feature branch won't see them and vice versa.

The save step is `actions/cache/save` with `if: always()`, deliberately split from the restore rather than using the combined `actions/cache`. The combined action skips its save when a job fails, which would discard the partial-delivery record described above — so a Discord failure on message 3 of 3 would repost messages 1 and 2 on the next run.

A `concurrency` group serialises runs, because two overlapping runs would race on the same state file and the loser's recorded tickets would be forgotten.

If you outgrow the cache's guarantees, the next step up is a private store (a private gist, S3, or a private companion repo) — **not** committing state to this public repo.

### Tests

```
python -m unittest discover -s zendesk_triage -v
```

Offline tests covering the window arithmetic, dedup partitioning, state round-trip and pruning, corrupt-state degradation, Discord line rendering and message chunking, defensive JSON parsing, and the retry/pagination behaviour with a stub session. No secrets or network access needed. They run in CI on any push or PR touching `zendesk_triage/`.

### Local Testing

Local runs use the same Anthropic API path as CI, so they need an `ANTHROPIC_API_KEY` alongside the Zendesk credentials. `--dry-run` prints the Discord payload instead of posting, so no webhook is needed:

```
pip install -r zendesk_triage/requirements.txt
export ZENDESK_SUBDOMAIN=... ZENDESK_EMAIL=... ZENDESK_API_TOKEN=... ANTHROPIC_API_KEY=...

# what CI runs, minus the Discord post and the state file
python zendesk_triage/triage.py --window-hours 48 --dry-run

# keep it cheap while iterating on the rendering
python zendesk_triage/triage.py --window-hours 12 --max-tickets 5 --dry-run

# or take the model out of the loop: dump the batch, classify it by hand,
# and feed the findings back in to render
python zendesk_triage/triage.py --dump-batch /tmp/batch.json --window-hours 48
python zendesk_triage/triage.py --findings /tmp/findings.json --dry-run
```

## Zendesk Resolve Positive Reviews

Weekly counterpart to the triage: it solves the 4-5★ AppFollow reviews that were never going to be actioned, so the unsolved backlog reflects work that actually exists. When this was written **5,253** reviews were unsolved — **4,812** of them still `new` — against **428** non-review unsolved tickets. Solving reviews was already being done by hand: **4,959** were already solved or closed.

> ⚠️ **This workflow writes to Zendesk.** A scheduled run always applies. A manual run is a **dry run** unless you tick `apply`, so the dispatch button cannot solve tickets by accident. Read the warning at the top of [resolve_reviews.py](zendesk_triage/resolve_reviews.py) before the first applied run.

### What it will and will not touch

Deliberately narrow, because a mis-aimed bulk status change is not recoverable by re-running:

- **App-store reviews only**, by the same detection the triage uses — `triage.is_store_review`, so the two can't drift apart. Every fetched ticket is re-checked locally, since the query can't express the rating.
- **Rated 4★ or better.** A fixed floor (`MIN_STARS`), not a flag — 3★ and below are what the triage reads as bug reports in disguise, so a lower floor would have this job close the reviews most worth looking at. A review whose stars can't be parsed from the subject is skipped, never solved.
- **`new` only** — untouched reviews. The other 441 unsolved reviews are `open`, and every one of a 100-ticket sample had an assignee, a group, and an `updated_at` past its `created_at`: something already acted on them, so a bulk status change has no business there. There is deliberately no flag to widen this.
- **`solved`, never `closed`.** Solved is reversible; closed is not.
- **Tagged** `auto-resolved-review`, so they stay identifiable and a trigger can exclude them, and annotated with a **private** note — a public comment would email the person who wrote the review.

That tag is also how a run is reviewed afterwards. An applied run prints — and posts — an agent-search link to what it just solved, so the set can be eyeballed, or found again and reopened, without reconstructing the query by hand:

```
Solved 7 of 7 ticket(s).
  review what changed: https://acme.zendesk.com/agent/search/1?type=ticket&q=tags%3Aauto-resolved-review%20status%3Asolved%20updated%3E2026-08-16
```

The date bound is yesterday rather than today because Zendesk's date search is day-granular and `updated>` is exclusive — today's date would filter out the very tickets the run just solved — and the spare day absorbs the account timezone the search interprets dates in. Since the job runs weekly, that window is this run and nothing else. A run that solved nothing links the tag without a date bound instead, so the link shows the job's history rather than landing on an empty search.

### Before the first applied run

Solving a ticket fires triggers and automations, and an AppFollow requester may carry a real email address. **A satisfaction survey trigger would email thousands of app-store reviewers.** Check Admin Center → Objects and rules → Business rules first, then do the first applied run with `--max-tickets 5` so the effects are observable before they're bulk.

### How it drains

No state file: a solved ticket drops out of the query, so runs are idempotent. Zendesk's search API caps at 1,000 results, so a run can never see more than that — the first few runs work the backlog down and after that the weekly schedule comfortably clears the ~420 reviews a week that arrive. `update_many` takes [100 ids per request](https://developer.zendesk.com/api-reference/ticketing/tickets/tickets/#update-many-tickets) and is asynchronous, so each batch's job is polled to completion and per-ticket failures fail the run rather than being reported as success.

### What it posts

Every applied run reports to the same Discord channel as the daily triage, so a job that bulk-edits tickets is visible where those tickets are already discussed:

> ✅ Marked **12** 4★ and **31** 5★ app-store reviews as solved in Zendesk.
> 🔍 [Review what changed](#)
> 📥 **4,769** more tickets match than this run looked at; the next run picks them up.

The rating split is the point — a bare total wouldn't say which reviews went. The tally counts the ids each bulk job **confirmed**, not the ids submitted, so the number is what Zendesk actually changed; a batch with per-ticket failures adds a line saying so, next to the count it contradicts. The leftover line appears only while there's a backlog left to drain.

**A run that solved nothing reports that too**, rather than staying quiet:

> 💤 No 4★ or better app-store reviews left to solve — looked at **48** untouched tickets.
> 🔍 [Everything this job has solved](#)

Silence would be indistinguishable from a job that has quietly stopped working — a broken query, a rotated token, a schedule that no longer fires — and this job exists to keep a number moving that nobody watches directly, so "looked, found nothing" is the half of the week worth hearing. The count of what it examined is what separates the two. Eligible reviews that all *failed* get their own wording (`None of the 3 eligible app-store reviews were solved`), because reporting that as a quiet week would dress a broken run up as a clean one.

A dry run prints the message it would have posted instead of posting it, and `--no-discord` solves without reporting. The message is a tally rather than a per-ticket list, so unlike the triage digest it can't spill into a second message.

### Required Secrets

`ZENDESK_SUBDOMAIN`, `ZENDESK_EMAIL`, `ZENDESK_API_TOKEN` — the same three the triage uses — plus `ZENDESK_DISCORD_WEBHOOK_URL`, the triage channel's own webhook, so the tally lands next to the digests it accounts for. Not the shared `DISCORD_WEBHOOK_URL`: a webhook is bound to the channel it was created in, and this job's *failures* still go there via the failure-notification workflow. No Claude credentials: it classifies nothing.

The webhook is resolved before the run fetches anything, so a missing secret stops it rather than letting it bulk-edit tickets it then can't report; a dry run doesn't need one.

### Schedule

Mondays at 05:00 UTC. Failures are reported through the Discord failure-notification workflow, which watches this workflow by name — renaming `Zendesk Resolve Positive Reviews` means updating the `workflows:` list in [`notify_failure.yml`](.github/workflows/notify_failure.yml) too.

## Workflow Failure Notificaiton

If a workflow fails and is in the list of workflows monitored by the failure notificaiton workflow, the failure notificaiton workflow will send a message to a discord webhook.

### Required Secrets

| Secret                | Description                        |
| --------------------- | ---------------------------------- |
| `DISCORD_WEBHOOK_URL` | Url for the Discord webhook        |
| `DISCORD_ROLE_ID`     | Discord role id to tag in messages |

### Trigger Test Notification

The failure notification can be triggered by manualy running the Test Failure Notification workflow.

