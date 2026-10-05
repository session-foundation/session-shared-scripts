# Crowdin Translation Sync

Downloads the approved translations from Crowdin, validates them, and publishes each
platform's strings: a pull request on session-android and session-ios, and a commit
straight onto session-localization's `main`, the TypeScript module Desktop and QA use.
Each client holding that module as a submodule then gets a pull request moving it to the
commit just pushed:

| Target | Repo | Base | Submodule |
| --- | --- | --- | --- |
| `desktop` | session-desktop | `dev` | `ts/localization` |
| `app` | session-app | `main` | `packages/localization/src/localization-src` |
| `website` | session-website | `main` | `lib/app_localization` |
| `appium` | session-appium | `main` | `run/localizer/lib` |
| `playwright` | session-playwright | `main` | `tests/localization/lib` |

| | |
| --- | --- |
| Runs | `session-ops-queue.timer`, Mon–Fri from 10:00 Australia/Melbourne, after `zendesk-digest` |
| Secrets | `/etc/session-ops/crowdin.env`: a read-only `CROWDIN_API_TOKEN`; `/etc/session-ops/publish.env` and the GitHub App key, to publish |
| Dry run | `session-ops run crowdin-sync --dry-run`: everything but the push, with each platform's diff in the journal |
| Re-run | `systemctl start session-ops@crowdin-sync.service`; one target with `session-ops run crowdin-sync -- --only ios` |
| Logs | `journalctl -u session-ops@crowdin-sync -n 100 --no-pager`; the run's downloads, parsed JSON and validation report under `/var/lib/session-ops/crowdin-sync/runs/`, for 14 days |

One process, in order:

1. **Download** every locale's XLIFF export, approved translations only, plus the
   non-translatable glossary terms.
2. **Parse and validate** into one JSON file. A validation error stops the run;
   `-- --skip-validation-errors` publishes anyway.
3. **For each platform**, independently, so one failing does not stop the others:
   a shallow, sparse checkout of just the paths its generator writes, the generator,
   then publishing. The alert says which platform failed and at which step.
4. **Bump each client's submodule** to the commit localization published, cloning only
   `.gitmodules` and moving the gitlink. A bump is skipped, and reported, when
   localization fails; with `--only` leaving localization out, it moves to `main`'s tip.

The pull requests come from `feature/update-crowdin-translations`, rebuilt from `dev`
each run and force-pushed: the branch is the job's, and nobody else commits to it. An
unchanged tree is not pushed again, and a run with nothing to change closes the pull
request and deletes the branch. Android's own CI validates its pull request, so no
Gradle build runs here. The bumps follow the same rules from `update-localization`,
rebuilt from each client's base; appium's and playwright's assertions follow the
strings, so their pull request may need test fixes before it merges.

## Publishing

Publishing authenticates as the GitHub App, which must be installed on every repo above
with contents and pull requests write: `GITHUB_APP_ID` in `publish.env` and its
key in `/etc/session-ops/github-app.pem`, and a run missing either exits naming it (a
dry run needs neither); see [publish.env.example](../../deploy/env/publish.env.example).
Commits are authored as `PUBLISH_GIT_AUTHOR`.

### Crowdin token scopes

Crowdin scopes personal access tokens per endpoint family, and each scope can be
read-only. A token missing one returns `403 Forbidden` on just those endpoints while
every other call keeps working. One read-only token covers everything here:

| Token | Scopes | Used by |
| --- | --- | --- |
| `CROWDIN_API_TOKEN`, keyring `translation-api-token` | read-only: Projects, Source files & strings, Translations, Glossaries | this sync and [the duplicate report](crowdin-duplicates.md) |

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

> **Note:** Plural strings skip variable/tag comparison because languages have different plural forms (English: 2, Arabic: 6, Russian: 4). It would be nice to add support for plural validation in the future.
