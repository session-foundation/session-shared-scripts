# Golden outputs

`units/dropins.txt` is every systemd drop-in `session-ops units` writes for `jobs.toml`, so
a schedule, account or sandbox change shows up in review as a diff. To accept a deliberate
change, rerun with `UPDATE_GOLDENS=1` and review the diff:

```sh
UPDATE_GOLDENS=1 uv run python -m unittest tests.ops.test_deploy
```
