# Golden outputs

Each directory holds what a job printed for a recorded set of API responses. A test replays
`responses.json` through the job and compares its output byte for byte, so a refactor that
changes a payload fails.

| directory   | job                                         | recording                                  |
| ----------- | ------------------------------------------- | ------------------------------------------ |
| `digest/`   | `github_prs/digest.py --dry-run`, 720 h     | live org, 2026-09-25, trimmed to read fields |
| `report/`   | `report_multiple_translations.py --locales de` | synthetic, shaped like Crowdin's API     |
| `download/` | `download_translations_from_crowdin.py` as the sync runs it | synthetic                  |

Requests are matched by method, URL, query and body rather than by order, because the
Crowdin scripts fan out across threads. A request the recording lacks fails the test and
names it.

To accept a deliberate change, rerun the suite with `UPDATE_GOLDENS=1` and review the diff:

```sh
cd github_prs && UPDATE_GOLDENS=1 python -m unittest test_golden
cd crowdin && UPDATE_GOLDENS=1 python -m unittest test_goldens
```
