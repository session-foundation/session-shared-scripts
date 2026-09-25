# Crowdin Translation Workflow

Automated workflow that downloads translations from Crowdin, validates them, and creates PRs for iOS and Android platforms and for the Typescript Localization Module for Desktop and QA.

| | |
| --- | --- |
| Runs | `.github/workflows/check_for_crowdin_updates.yml`, Mondays 00:00 UTC, on GitHub Actions until it moves to the host |
| Secrets | `CROWDIN_API_TOKEN`, read-only; `CROWDIN_PR_TOKEN`, a GitHub token that can push branches and open pull requests on the platform repos |
| Dry run | run the workflow with `UPDATE_PULL_REQUESTS=false` |
| Re-run | Actions → Check for Crowdin Updates → Run workflow |

## Required Secrets

| Secret              | Description                                             |
| ------------------- | ------------------------------------------------------- |
| `CROWDIN_API_TOKEN` | Crowdin personal access token (see scopes below)         |
| `CROWDIN_PR_TOKEN`  | GitHub token with PR creation permissions                |

### Crowdin token scopes

Crowdin scopes personal access tokens per endpoint family, and each scope can be
read-only. A token missing one returns `403 Forbidden` on just those endpoints while
every other call keeps working. Two tokens cover everything here:

| Token | Scopes | Used by |
| --- | --- | --- |
| `CROWDIN_API_TOKEN`, keyring `translation-api-token` | read-only: Projects, Source files & strings, Translations, Glossaries | this sync, [the duplicate report](crowdin-duplicates.md) and its relay |
| `CROWDIN_PROOFREADER_TOKEN`, keyring `proofreader-api-token` | Projects and Source files & strings read-only; Translations read and write | `crowdin-approve-strings` only |

> **Note:** Scopes only cap what a token may do — they don't grant anything the
> token's Crowdin account can't already do, so the proofreader token's account also
> needs a project role that can approve.

## Workflow Inputs

| Input                    | Default | Description                              |
| ------------------------ | ------- | ---------------------------------------- |
| `UPDATE_PULL_REQUESTS`   | `true`  | Create/update PRs for all platforms      |
| `SKIP_VALIDATION_ERRORS` | `false` | Continue even if string validation fails |

## Schedule

Runs automatically every Monday at 00:00 UTC.

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
