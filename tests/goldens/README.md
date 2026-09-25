# Golden outputs

Each directory holds what a job printed for a recorded set of API responses. A test replays
`responses.json` through the job and compares its output byte for byte, so a refactor that
changes a payload fails.

| directory   | job                                         | recording                                  |
| ----------- | ------------------------------------------- | ------------------------------------------ |
| `digest/`   | `github-prs-digest --dry-run`, 720 h        | live org, 2026-09-25, trimmed to read fields |
| `report/`   | `crowdin-report-duplicates --locales de`    | synthetic, shaped like Crowdin's API     |
| `download/` | `crowdin-download` as the sync runs it      | synthetic                  |

Requests are matched by method, URL, query and body rather than by order, because the
Crowdin scripts fan out across threads. A request the recording lacks fails the test and
names it.

To accept a deliberate change, rerun the suite with `UPDATE_GOLDENS=1` and review the diff:

```sh
UPDATE_GOLDENS=1 uv run python -m unittest tests.github_prs.test_digest_golden
UPDATE_GOLDENS=1 uv run python -m unittest tests.crowdin.test_crowdin_goldens
```
