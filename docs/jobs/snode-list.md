# Static Snode List

Copies `service-nodes-cache.json` from session-desktop-dynamic-assets into session-ios
as `Session/Meta/service-nodes-cache.json`: the list a new client falls back on when it
cannot reach the seed nodes. A change opens, or updates, a pull request from
`feature/update-static-snode-list`.

| | |
| --- | --- |
| Runs | `session-ops-queue.timer`, Mon–Fri from 10:00 Australia/Melbourne, after `crowdin-sync`; the source updates at 10:00 UTC |
| Secrets | `/etc/session-ops/publish.env` and the GitHub App key; `CROWDIN_DISCORD_WEBHOOK_URL` from `crowdin.env` for the summary |
| Dry run | `session-ops run snode-list --dry-run` |
| Re-run | `systemctl start session-ops@snode-list.service` |
| Logs | `journalctl -u session-ops@snode-list -n 50 --no-pager` |

The file is committed exactly as fetched, but only once it holds service nodes, each
with an IP and a key: an error page or an empty list published as the fallback would
strand exactly the clients it exists for. The
branch follows the same rules as [the translation sync's](crowdin-sync.md): rebuilt
from `dev`, not pushed when unchanged, retired when `dev` already has the list.

Each run posts one line to the translations channel: how many nodes in the list have
`requested_unlock_height` set, meaning they asked to exit, and what happened to the pull
request. A failed run reports there too.
