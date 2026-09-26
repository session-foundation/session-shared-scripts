# Session Shared Scripts

Session Foundation's scheduled jobs and webhooks, and the scripts the platform repos
share for translations. One package, `session_ops`, deployed to one self-hosted box:
[deploy/README.md](deploy/README.md) installs it.

## Jobs

| Job | What it does | Doc |
| --- | --- | --- |
| `zendesk-digest` | Weekday Discord digest of the Zendesk tickets awaiting a reply, after closing positive app-store reviews | [zendesk-digest](docs/jobs/zendesk-digest.md) |
| `zendesk-relay` | Drafts and sends Zendesk replies from `claude:` private notes | [zendesk-relay](docs/jobs/zendesk-relay.md) |
| `github-prs-digest` | Weekday Discord digest of open pull requests from outside contributors | [github-prs-digest](docs/jobs/github-prs-digest.md) |
| `crowdin-duplicates` | Crowdin string slots holding more than one translation, as they open and close | [crowdin-duplicates](docs/jobs/crowdin-duplicates.md) |
| `crowdin-sync` | Weekly: Crowdin translations into pull requests on iOS, Android and the localization module | [crowdin-sync](docs/jobs/crowdin-sync.md) |
| `snode-list` | Daily: the fallback service node list into session-ios | [snode-list](docs/jobs/snode-list.md) |
| `release-stats` | On demand: download counts of the latest releases | [release-stats](docs/jobs/release-stats.md) |
| `session-ops-silence` | Discord alerts for a job that failed, or stopped running | [session-ops-silence](docs/jobs/session-ops-silence.md) |

Run by hand, not scheduled: [`sogs-ban` and `sogs-perms`](docs/tools/sogs-ban.md) for
community bans.

## Development

Every job is a console script declared in [pyproject.toml](pyproject.toml), and one
lockfile pins what all of them run.

```sh
uv sync                                               # .venv with every dependency
uv run python -m unittest discover -s tests -t .      # every suite
uv run ruff check .
```

`tests/sogs` skips itself unless `session_util` is importable; see
[its dependencies](docs/tools/sogs-ban.md#dependencies) for why it is not on PyPI.
[tests/goldens](tests/goldens/README.md) pins each job's output byte for byte.

## Workflow Failure Notificaiton

If a workflow fails and is in the list of workflows monitored by the failure notificaiton workflow, the failure notificaiton workflow will send a message to a discord webhook.

### Required Secrets

| Secret                | Description                 |
| --------------------- | --------------------------- |
| `DISCORD_WEBHOOK_URL` | Url for the Discord webhook |

### Trigger Test Notification

The failure notification can be triggered by manualy running the Test Failure Notification workflow.

