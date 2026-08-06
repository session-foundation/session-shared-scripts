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

Claude reviews recently-created unsolved Zendesk tickets via the API and posts a summary to Discord that links back to each original ticket and highlights the ones worth looking into. For each ticket it assigns a category, infers severity, guesses a likely root cause, identifies platform and app version, groups likely duplicates into clusters, and ranks by priority.

### Categories

`CATEGORY_SPECS` in [triage.py](zendesk_triage/triage.py) is the single source of truth — the schema enum, the Discord labels, the urgency colours, and the prompt guidance are all derived from it, so adding a category is one edit.

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

The first three are **urgent categories**: they are not bugs, so the model rates their severity `not_applicable`. Colouring by severity alone painted them the calmest blue and sorted them last, so category urgency wins — they render dark red, sort ahead of everything else, and cannot be pushed out of the digest by the display cap.

### App-store review filtering

73% of tickets are AppFollow-imported app-store reviews, and 71% of those are 5★ — 59% of *all* tickets are 4-5★ reviews that are never actionable. Those are counted, not classified, cutting the batch roughly 60% (a real run: 48 fetched → 20 classified).

Detection uses the Zendesk `via.channel`, which identified reviews with no false positives in a 3,662-ticket sample (2,656/2,656). **Not** tags — only 287 of those reviews carried the `app-store` tag. Reviews whose star rating can't be parsed are kept rather than dropped. Use `--include-positive-reviews` to disable, or `--review-star-floor` to move the threshold.

### Content-free tickets

Twitter DM tickets arrive with `description` identical to `subject` — both just `"Conversation with <handle>"` — which is 15% of non-review tickets and unclassifiable as fetched. For those only, `hydrate_descriptions` fetches a page of up to 10 comments and joins every body that differs from the subject into the description; later replies often carry the actual detail. Hydration is an enrichment, so an HTTP error or an unreachable endpoint leaves the ticket as-is rather than failing the run (`--no-hydrate` to skip it entirely).

The script (`zendesk_triage/triage.py`) fetches the tickets in a rolling time window, sends the whole batch to Claude in one structured-output request, and posts Discord embeds: a summary embed plus one embed per highlighted ticket (linking to the ticket in Zendesk).

The summary embed accounts for the batch in full, so nothing is dropped silently:

```
Analyzed **2** of **47** tickets in the window (created in the past 2 days). Skipped **45** already reported and unchanged.
Backlog: **5,609** unsolved tickets in total (not triaged).
**1** worth looking into. 🔄 **1** changed since last reported.
```

> **Scope:** the window covers tickets *created* recently, so the long tail of older unsolved tickets is counted in the backlog line but not triaged. That is deliberate — the job is a new-ticket digest, not a backlog sweep.

### Deduplication

The daily window is 48h, so consecutive runs overlap. A state file (`--state`) records each reported ticket's Zendesk `updated_at`, giving three outcomes per ticket:

| Ticket | Outcome |
| ------ | ------- |
| Not seen before | Analyzed and reported |
| Seen, `updated_at` unchanged | **Skipped before the model call** — costs no tokens |
| Seen, `updated_at` moved | Re-analyzed, reported, and flagged 🔄 in the embed title |

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
| `ANTHROPIC_API_KEY`   | Claude API key                                          |
| `DISCORD_WEBHOOK_URL` | Discord webhook (reused from the failure-notification setup) |

### Optional Configuration

| Setting                | Where            | Default                                                 | Description |
| ---------------------- | ---------------- | ------------------------------------------------------- | ----------- |
| `--window-hours`       | workflow input / flag | `48`                                               | Analyze unsolved tickets created in the last N hours |
| `--state`              | flag             | *(unset)*                                               | Dedup state file. The workflow points this at the cached `.triage-state/seen.json` |
| `--state-retention-days` | flag           | `30`                                                    | Forget state entries older than N days |
| `ZENDESK_QUERY`        | env / `--query`  | *(unset)*                                               | Explicit Zendesk search query. Overrides `--window-hours` entirely |
| `ZENDESK_TRIAGE_MODEL` | repo variable / `--model` | `claude-opus-4-8`                             | Set to a cheaper model (e.g. `claude-haiku-4-5`) to reduce cost on large batches |
| `--max-tickets`        | workflow input / flag | `1000` (workflow) / `100` (flag)                   | Runaway guard on tickets analyzed per run, **not** a batch size. The workflow passes `1000`; a bare `python triage.py` uses the script's own `DEFAULT_MAX_TICKETS` of `100`. Zendesk's search API caps a query at 1000 results, so higher values don't fetch more |
| `--batch-size`         | flag             | `400`                                                   | Split batches larger than this across multiple requests |
| `--review-star-floor`  | flag             | `3`                                                     | Classify app-store reviews at or below N stars; count the rest |
| `--include-positive-reviews` | flag       | off                                                     | Classify every review, including 4-5★ ones |
| `--no-hydrate`         | flag             | off                                                     | Skip fetching comments for content-free tickets |
| `--effort`             | flag             | `medium`                                                | Claude reasoning effort (`low`–`max`) |

#### Batch size vs. ticket cap

These do different jobs, and conflating them is how you get a silently truncated digest:

- **`--max-tickets`** bounds how much of the Zendesk result set is fetched. At the workflow's 1000 it never binds on a 48h window (~45 tickets); it exists so a spam flood or a wide `reset_state` backfill can't run away. 1000 is also [Zendesk's own search result limit](https://developer.zendesk.com/api-reference/ticketing/ticket-management/search/#results-limit) — the API returns `422` for any page past it, so the fetch stops at 1000 regardless of what you pass, and reports the matched-vs-analyzed gap rather than failing.
- **`--batch-size`** bounds how many tickets go into a *single* model request. Anything larger is split across requests and the findings are concatenated.

The split is necessary because output tokens, not context, are the binding constraint. Measured on real tickets: **~118 input tokens and ~102 output tokens per ticket**, with adaptive thinking drawing from the same `max_tokens` budget.

| Batch | Input | Output needed | Fits in one request? |
| ----- | ----- | ------------- | -------------------- |
| 45 (typical daily) | ~5K | ~5K | Yes |
| 400 (`--batch-size`) | ~47K | ~41K | Yes, with room for thinking |
| 1000 (`--max-tickets`) | ~118K | ~102K | **No** — leaves only ~26K of the 128K output ceiling for thinking |

If a single request ever does hit the ceiling, the script exits with that explicit reason rather than failing on an incomplete-JSON parse error.

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

Offline tests covering the window arithmetic, dedup partitioning, state round-trip and pruning, corrupt-state degradation, Discord embed rendering and chunking, defensive JSON parsing, and the retry/pagination behaviour with a stub session. No secrets or network access needed. They run in CI on any push or PR touching `zendesk_triage/`.

### Local Testing

```
pip install -r zendesk_triage/requirements.txt
export ZENDESK_SUBDOMAIN=... ZENDESK_EMAIL=... ZENDESK_API_TOKEN=... ANTHROPIC_API_KEY=...

# fetch + analyze, print the Discord payload, post nothing
python zendesk_triage/triage.py --window-hours 48 --dry-run
```

No `ANTHROPIC_API_KEY`? Two debug backends skip the Anthropic API entirely:

```
# classify via the local `claude` CLI (authenticates as Claude Code)
python zendesk_triage/triage.py --backend claude-cli --window-hours 48 --dry-run

# or dump the batch, classify it by hand, and feed the findings back
python zendesk_triage/triage.py --dump-batch /tmp/batch.json --window-hours 48
python zendesk_triage/triage.py --backend file --findings /tmp/findings.json --dry-run
```

The `claude-cli` backend has no structured-output enforcement, so its field values are looser than the API path's (e.g. `"en"` where the schema asks for `"English"`), and each invocation carries ~25K tokens of Claude Code system-prompt overhead. Use it for debugging, not for scheduled runs.

## Workflow Failure Notificaiton

If a workflow fails and is in the list of workflows monitored by the failure notificaiton workflow, the failure notificaiton workflow will send a message to a discord webhook.

### Required Secrets

| Secret                | Description                        |
| --------------------- | ---------------------------------- |
| `DISCORD_WEBHOOK_URL` | Url for the Discord webhook        |
| `DISCORD_ROLE_ID`     | Discord role id to tag in messages |

### Trigger Test Notification

The failure notification can be triggered by manualy running the Test Failure Notification workflow.

