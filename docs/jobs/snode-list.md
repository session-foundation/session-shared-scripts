# Static Snode List

Fetches the service node list from the seed nodes and publishes it wherever a client
bundles it, as the list a new client falls back on when it cannot reach them:

| Target | Repo | What it gets |
| --- | --- | --- |
| `dynamic-assets` | session-desktop-dynamic-assets | `service-nodes-cache.json`, committed straight onto `main` |
| `desktop` | session-desktop | a pull request from `update-dynamic-assets` moving its `dynamic_assets` submodule to that commit |
| `ios` | session-ios | `Session/Meta/service-nodes-cache.json`, in a pull request from `feature/update-static-snode-list` |

| | |
| --- | --- |
| Runs | `session-ops-queue.timer`, Mon–Fri from 10:00 Australia/Melbourne, after `crowdin-sync` |
| Secrets | `/etc/session-ops/publish.env` and the GitHub App key, installed on the three repos above; `CROWDIN_DISCORD_WEBHOOK_URL` from `crowdin.env` for the summary |
| Dry run | `session-ops run snode-list --dry-run` |
| Re-run | `systemctl start session-ops@snode-list.service` |
| Logs | `journalctl -u session-ops@snode-list -n 50 --no-pager` |

The seeds are asked in turn until one answers with at least 20 active nodes, each
carrying every field the clients read; anything less publishes nothing, since an empty
or broken fallback would strand exactly the clients it exists for. Nodes without a
public IP are dropped, and the rest sorted by `pubkey_ed25519` so a run's diff is only
what changed on the network.

The targets are independent, except that Desktop's bump needs dynamic-assets to have
published. The pull requests follow [the translation sync's](crowdin-sync.md) rules:
rebuilt from `dev`, not pushed when unchanged, retired when `dev` already has it.

Each run posts one message to the translations channel: how many nodes have
`requested_unlock_height` set, meaning they asked to exit, then what happened in each
repo. A failed target is reported there too.
