# Failure and Silence Alerts

Every unit on the host reports two ways of going wrong to Discord. Success is quiet.

| | |
| --- | --- |
| Runs | `OnFailure=` on every unit; `session-ops-silence.timer`, hourly |
| Secrets | `/etc/session-ops/env`: `ALERT_DISCORD_WEBHOOK_URL`; each job's own env file for its failure alerts |
| Dry run | `session-ops-silence --dry-run` prints every job's last success and the alert it would post |
| Re-run | `systemctl start session-ops-silence.service` |
| Logs | `journalctl -u session-ops-silence -n 50 --no-pager` |

**A run that failed.** Each unit's `OnFailure=` starts `session-ops-alert` with the
unit's name, which posts the unit, the host, the last line the unit logged and the
`journalctl` command to read the rest. A dead Claude Code login is named as such,
since re-running the job does not fix it.

**A run that never happened.** A timer left disabled, a unit renamed, a host down
through a whole schedule: nothing fails, so nothing alerts. Each scheduled unit touches
`/var/lib/session-ops/stamps/<name>` from `ExecStartPost=` when it succeeds, and
`session-ops-silence` posts every job in
[`jobs.toml`](../../src/session_ops/monitor/jobs.toml) whose stamp is older than its
`max_age_hours`, repeating daily while it stays quiet. A job with no stamp is timed from
the first check that found it missing, so installing the checker alerts on nothing.

A new scheduled job needs its `ExecStartPost=` line and a `jobs.toml` entry;
`tests/monitor/test_silence.py` fails without either.
