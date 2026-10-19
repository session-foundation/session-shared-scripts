# Token Expiry Alerts

Posts to the alerts channel 14 days, 7 days and 24 hours before a token the jobs use
expires, and once when it has. Quiet otherwise. Times are UTC; a date without one, such
as Crowdin's, counts from 00:00 UTC that day.

| | |
| --- | --- |
| Runs | `session-ops@token-expiry.timer`, daily at 09:00 Melbourne |
| Secrets | `/etc/session-ops/alerts.env`: `ALERT_DISCORD_WEBHOOK_URL`; `github-prs.env` for the GitHub token |
| Dates | `/etc/session-ops/expiry.toml`, from [`expiry.toml.example`](../../deploy/env/expiry.toml.example) |
| Dry run | `session-ops run token-expiry --dry-run` prints each token's expiry and the alert it would post. On the host, through `systemd-run` as in [deploy/README.md](../../deploy/README.md). |
| Logs | `journalctl -u session-ops@token-expiry -n 50 --no-pager` |

| Token | Expiry from |
| --- | --- |
| `GITHUB_PRS_TOKEN` | GitHub's `github-authentication-token-expiration` header; nothing to update on rotation |
| `CROWDIN_API_TOKEN` | `expiry.toml`: the Expires column at https://crowdin.com/settings#api-key |

`CROWDIN_API_TOKEN` is reported on every daily run until `expiry.toml` has a date or
`"never"` for it.

The Zendesk API token, the Claude API key, the Discord webhooks and the GitHub App key
do not expire.
