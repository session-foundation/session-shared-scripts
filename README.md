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

The script (`zendesk_triage/triage.py`) fetches the tickets in a rolling time window, classifies the whole batch in one schema-enforced request through the `claude` CLI, and posts Discord embeds: a summary embed plus one embed per highlighted ticket (linking to the ticket in Zendesk).

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
| `ANTHROPIC_API_KEY`   | Claude API key — see [Claude authentication](#claude-authentication) |
| `DISCORD_WEBHOOK_URL` | Discord webhook (reused from the failure-notification setup) |

### Claude Authentication

The workflow classifies through the Anthropic API (`--backend api`) with an `ANTHROPIC_API_KEY`. That keeps CI on an organization-owned credential that doesn't draw on any individual's subscription quota, and keeps the runner free of a ~270MB Claude Code download. The `claude-cli` backend stays in the script for [local runs](#local-testing) and is not used in CI.

If you go looking for that key and can't find one: an API key only exists inside a **Claude Console organization** (`platform.claude.com`), which is a separate organization from a claude.ai Pro/Max/Team/Enterprise subscription, with its own membership and billing. A claude.ai admin console has no API keys in it at all, so the usual answer is that no Console organization exists yet rather than that you're missing a permission.

### Optional Configuration

| Setting                | Where            | Default                                                 | Description |
| ---------------------- | ---------------- | ------------------------------------------------------- | ----------- |
| `--window-hours`       | workflow input / flag | `48`                                               | Analyze unsolved tickets created in the last N hours |
| `--state`              | flag             | *(unset)*                                               | Dedup state file. The workflow points this at the cached `.triage-state/seen.json` |
| `--state-retention-days` | flag           | `30`                                                    | Forget state entries older than N days |
| `ZENDESK_QUERY`        | env / `--query`  | *(unset)*                                               | Explicit Zendesk search query. Overrides `--window-hours` entirely |
| `ZENDESK_TRIAGE_MODEL` | repo variable / `--model` | `claude-opus-5`                                         | Overrides the model. Takes a full id, or an alias (`opus`, `sonnet`, `haiku`) which `--backend api` maps to an id via `API_MODEL_ALIASES`. **Leave it unset for normal operation** — the default lives in the script so there's one place to change it |
| `--backend`            | flag             | `claude-cli` (flag) / `api` (workflow)                  | Where classification happens: `claude-cli` for local runs, `api` for CI, or `file` to render findings classified elsewhere |
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

Offline tests covering the window arithmetic, dedup partitioning, state round-trip and pruning, corrupt-state degradation, Discord embed rendering and chunking, defensive JSON parsing, and the retry/pagination behaviour with a stub session. No secrets or network access needed. They run in CI on any push or PR touching `zendesk_triage/`.

### Local Testing

Locally the default backend is `claude-cli`, which reuses your own Claude Code login — so no Claude credential is needed, only the Zendesk ones:

```
pip install -r zendesk_triage/requirements.txt
export ZENDESK_SUBDOMAIN=... ZENDESK_EMAIL=... ZENDESK_API_TOKEN=...

# fetch + classify through your Claude Code login, print the payload, post nothing
python zendesk_triage/triage.py --window-hours 48 --dry-run

# exercise exactly what CI runs
ANTHROPIC_API_KEY=... python zendesk_triage/triage.py --backend api --window-hours 48 --dry-run

# or dump the batch, classify it by hand, and feed the findings back
python zendesk_triage/triage.py --dump-batch /tmp/batch.json --window-hours 48
python zendesk_triage/triage.py --backend file --findings /tmp/findings.json --dry-run
```

Two things about the local `claude-cli` path:

- **It spends your own subscription usage**, shared with your interactive Claude Code and chat usage — one invocation per chunk, so a 48h window is a single call (~$0.015 of equivalent usage on a small batch).
- **An exported `ANTHROPIC_API_KEY` silently takes over.** It [outranks your login](https://code.claude.com/docs/en/authentication#authentication-precedence) in Claude Code's credential precedence, and in `-p` mode a key that is present is always used — so with one exported in your shell, `--backend claude-cli` bills the API rather than using your subscription. `unset ANTHROPIC_API_KEY` if you want the subscription path.
- **It needs Claude Code v2.1.205 or newer** for `--json-schema`. On an older CLI the run exits with "no structured_output" naming that version; check `claude --version`.

#### How the `claude-cli` invocation is locked down

Ticket text is written by strangers, so the CLI is invoked with as little around it as possible: our own `--system-prompt` in place of Claude Code's, `--setting-sources ""` (no hooks, plugins, skills, allow-rules or `CLAUDE.md` from either your machine or this repo), `--strict-mcp-config` with no config (no MCP servers), and an explicit `--disallowed-tools` list. A session then exposes one tool, `StructuredOutput`, and no MCP servers. Removing the agent preamble and the tool definitions is also what makes this path cheap.

Three findings from `v2.1.218` that explain why it's written that way — all worth re-testing after a CLI upgrade:

- **`--disallowed-tools "*"` can't be used**, tempting as it is. It empties the surface, but `--json-schema` is itself implemented as a `StructuredOutput` tool, so the wildcard denies that too and the run returns prose with no `structured_output`. Allow-listing `StructuredOutput` alongside the wildcard leaves the tool present but still doesn't produce structured output.
- **`--permission-mode dontAsk` is not a boundary.** A session with no allow rules still ran `Bash(echo …)`, because the mode permits a read-only command set. It's kept as a backstop, not as the control.
- **The deny list is therefore by name, and will go stale** as tools are added. Naming only the obvious ones (`Bash`, `Read`, `Write`, …) left 19 others live, including several with outward side effects. To see what a session really exposes, read the `init` event: `echo hi | claude -p --output-format stream-json --verbose [flags] | grep '"subtype":"init"'`.

One more, on the flag not used:

- **Do not add `--bare`.** It's otherwise the right flag for a scripted call (it skips hook, plugin, MCP and `CLAUDE.md` discovery, so the runner behaves the same as your laptop), but bare mode reads `ANTHROPIC_API_KEY` or an `apiKeyHelper` **only** — it never touches OAuth credentials, which is exactly what both CI and your local login use. See [bare mode](https://code.claude.com/docs/en/headless#start-faster-with-bare-mode); the docs say it will become the default for `-p` in a future release, so this is worth re-checking on CLI upgrades.

## Workflow Failure Notificaiton

If a workflow fails and is in the list of workflows monitored by the failure notificaiton workflow, the failure notificaiton workflow will send a message to a discord webhook.

### Required Secrets

| Secret                | Description                        |
| --------------------- | ---------------------------------- |
| `DISCORD_WEBHOOK_URL` | Url for the Discord webhook        |
| `DISCORD_ROLE_ID`     | Discord role id to tag in messages |

### Trigger Test Notification

The failure notification can be triggered by manualy running the Test Failure Notification workflow.

