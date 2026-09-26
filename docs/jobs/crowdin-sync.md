# Crowdin Translation Sync

Downloads the approved translations from Crowdin, validates them, and publishes each
platform's strings: a pull request on session-android and session-ios, and a commit
straight onto session-localization's `main`, the TypeScript module Desktop and QA use.

| | |
| --- | --- |
| Runs | `session-ops@crowdin-sync.timer`, Mondays 13:00 Australia/Melbourne |
| Secrets | `/etc/session-ops/crowdin.env`: a read-only `CROWDIN_API_TOKEN`; `/etc/session-ops/publish.env` and the GitHub App key, to publish |
| Dry run | `session-ops run crowdin-sync --dry-run`: everything but the push, with each platform's diff in the journal |
| Re-run | `systemctl start session-ops@crowdin-sync.service`; one platform with `session-ops run crowdin-sync -- --only ios` |
| Logs | `journalctl -u session-ops@crowdin-sync -n 100 --no-pager`; the run's downloads, parsed JSON and validation report under `/var/lib/session-ops/crowdin-sync/runs/`, for 14 days |

One process, where the workflow was eight jobs passing artefacts:

1. **Download** every locale's XLIFF export, approved translations only, plus the
   non-translatable glossary terms.
2. **Parse and validate** into one JSON file. A validation error stops the run;
   `-- --skip-validation-errors` publishes anyway.
3. **For each platform**, independently, so one failing does not stop the others:
   a shallow, sparse checkout of just the paths its generator writes, the generator,
   then publishing. The alert says which platform failed and at which step.

The pull requests come from `feature/update-crowdin-translations`, rebuilt from `dev`
each run and force-pushed: the branch is the job's, and nobody else commits to it. An
unchanged tree is not pushed again, and a run with nothing to change closes the pull
request and deletes the branch. Android's own CI validates its pull request, so no
Gradle build runs here.

## Publishing

Publishing authenticates as a GitHub App when one is set up, or with a token string
until then; see `publish.env` in [deploy/README.md](../../deploy/README.md#secrets).
Commits are authored as `PUBLISH_GIT_AUTHOR`.

### Crowdin token scopes

Crowdin scopes personal access tokens per endpoint family, and each scope can be
read-only. A token missing one returns `403 Forbidden` on just those endpoints while
every other call keeps working. One read-only token covers everything here:

| Token | Scopes | Used by |
| --- | --- | --- |
| `CROWDIN_API_TOKEN`, keyring `translation-api-token` | read-only: Projects, Source files & strings, Translations, Glossaries | this sync, [the duplicate report](crowdin-duplicates.md) and its relay |

## Validation Rules

### All Strings (including plurals)

- **Valid `{variable}` syntax** - No broken braces (`{`, `}`, `{}`, `{ space }`)
- **Allowed HTML tags only** - Only `<b>`, `<br/>`, `<span>`
- **Valid tag syntax** - No malformed `<` (e.g., `<script>`, `<123>`)

### Non-Plural Strings Only

- **Variables match English** - Same `{variables}` as source locale
- **Tag count matches English** - Same number of tags (warning only)

### All Locales

- **No extra keys** - No strings that don't exist in English

> **Note:** Plural strings skip variable/tag comparison because languages have different plural forms (English: 2, Arabic: 6, Russian: 4). It would be nice to add suppot for plural validation in the future.
