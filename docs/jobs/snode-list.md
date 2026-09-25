# Static Snode List

Copies `service-nodes-cache.json` from session-desktop-dynamic-assets into session-ios
as `Session/Meta/service-nodes-cache.json`: the list a new client falls back on when it
cannot reach the seed nodes. A change opens, or updates, a pull request from
`feature/update-static-snode-list`.

| | |
| --- | --- |
| Runs | `session-ops@snode-list.timer`, daily 10:30 UTC, half an hour after the source updates |
| Secrets | `/etc/session-ops/publish.env` and the GitHub App key; the list itself is public |
| Dry run | `session-ops run snode-list --dry-run` |
| Re-run | `systemctl start session-ops@snode-list.service` |
| Logs | `journalctl -u session-ops@snode-list -n 50 --no-pager` |

The file is committed exactly as fetched, but only once it parses as JSON: an error
page published as the fallback would strand exactly the clients it exists for. The
branch follows the same rules as [the translation sync's](crowdin-sync.md): rebuilt
from `dev`, not pushed when unchanged, retired when `dev` already has the list.
