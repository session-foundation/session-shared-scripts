# Discord Commands for the Jobs

| | |
| --- | --- |
| Runs | `session-ops-discord.service`, always on, behind nginx at `POST /discord/interactions` |
| Secrets | `/etc/session-ops/discord.env`: the app's public key, the server and who may run the commands |
| Setup | [deploy/README.md](../../deploy/README.md#discord-commands) |
| Logs | `journalctl -u session-ops-discord -n 50 --no-pager`: who ran what, and each outcome |

| Command | Does |
| --- | --- |
| `/run job:<job>` | Starts `session-ops@<job>.service` for a job `jobs.toml` marks `discord = true`. Refused while that job or any queued job is running or waiting to. The job posts its outcome in its own channel. |
| `/mau-upload file:<csv>` | Downloads the attachment, checks it parses as the Play Console export, and moves it into [mau](mau.md)'s inbox, whose path unit runs the job. A file it would reject is refused in Discord and never reaches the inbox. |

The app has no gateway connection and only the `applications.commands` scope, so Discord
sends it the commands run against it and nothing else: no messages, members or other
channels. Each request carries who ran it, their roles, the server, the channel and the
options chosen.

A request is refused unless Discord signed it in the last five minutes, it comes from
`DISCORD_GUILD_ID`, and its author is in `ALLOWED_USER_IDS` or holds a role in
`ALLOWED_ROLE_IDS`. Both lists empty refuses everybody. The commands are registered
hidden from everyone but server administrators, and the server's Integrations settings
show them to the right role; that only hides them, and these checks are what refuse
everyone else, administrators included.

The relay runs as `opsbot`. The polkit rule `install.sh` writes from `jobs.toml` lets that
account start the `discord = true` jobs and nothing else, and the mau inbox is the one
path it can write.
