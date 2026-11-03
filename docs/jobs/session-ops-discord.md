# Discord Commands for the Jobs

| | |
| --- | --- |
| Runs | `session-ops-discord.service`, always on, behind nginx at `POST /discord/interactions` |
| Secrets | `/etc/session-ops/discord.env`: the app's public key, the server and who may run the commands |
| Setup | [deploy/README.md](../../deploy/README.md#discord-commands) |
| Logs | `journalctl -u session-ops-discord -n 50 --no-pager`: who ran what, and each outcome |

| Command | Does |
| --- | --- |
| `/run job:<job>` | Starts `session-ops@<job>.service` for a job `jobs.toml` marks `discord = true` by writing `/run/session-ops/requests/<job>`, which the job's path unit watches, then waiting up to 1.5 seconds for the unit to start. If it has not, the reply says the job did not start. Refused while that job or any queued job is running or waiting to, or when the queue's next run is sooner than the job's timeout from now, or while another command is starting a job. The job posts its outcome in its own channel. |
| `/versions` | One line per platform from [store-versions](store-versions.md)' last run: the store's version, GitHub's, ⚠️ while a warning is out, and since when a source has not read. Visible to the channel, and noting when that run is over 15 minutes old. |

The app has no gateway connection and only the `applications.commands` scope, so Discord
sends it the commands run against it and nothing else: no messages, members or other
channels. Each request carries who ran it, their roles, the server, the channel and the
options chosen.

A request is refused unless Discord signed it in the last five minutes, it comes from
`DISCORD_GUILD_ID` and, when `ALLOWED_CHANNEL_IDS` is set, one of its channels, and its
author is in `ALLOWED_USER_IDS` or holds a role in `ALLOWED_ROLE_IDS`. Both lists empty
refuses everybody, administrators included. Both commands are shown to every member, since
Discord lets only someone who can use a command change who sees it; the server's
Integrations settings can narrow the channels. `/versions` only reads, so it skips the
author check.

The relay runs as `opsbot`, which can start no unit itself. It can write only to
`/run/session-ops/requests`, where `session-ops units` writes a `PathChanged=` watch for
each `discord = true` job and nothing else, so a file of any other name starts nothing.
It reads store-versions' `state.json`, which that job writes 0644. A
write to a request starts its job once, so nothing removes requests, and root never acts
on a file `opsbot` controls. The job runs as its own account with its own unit's
settings. The request carries nothing.
