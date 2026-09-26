# Failure and Silence Alerts

Every unit on the host reports two ways of going wrong to Discord. Success is quiet.

| | |
| --- | --- |
| Runs | the run itself, and `OnFailure=` on every unit; `session-ops@session-ops-silence.timer`, hourly |
| Secrets | `/etc/session-ops/alerts.env`: `ALERT_DISCORD_WEBHOOK_URL`; each job's own env file for its own failures |
| Dry run | `session-ops-silence --dry-run` prints every job's last success and the alert it would post |
| Re-run | `systemctl start session-ops@session-ops-silence.service` |
| Logs | `journalctl -u session-ops@session-ops-silence -n 50 --no-pager` |

**A run that failed.** `session-ops run` reports its own failure: the job, the host,
the step it was on, one sentence of error with secrets scrubbed, each target's result,
and the commands to read the journal and re-run it. It records the invocation it
reported, and `OnFailure=` starts `session-ops-alert`, which stays quiet for that
invocation and otherwise posts the unit, the host and the last line it logged: a run
killed, timed out, or unable to reach Discord, and the relays, which are not jobs. A
dead Claude Code login is named as such, since re-running the job does not fix it.

**A run that never happened.** A timer left disabled, a unit renamed, a host down
through a whole schedule: nothing fails, so nothing alerts. Each scheduled unit touches
`/var/lib/session-ops/stamps/<name>` from `ExecStartPost=` when it succeeds, and
`session-ops-silence` posts every job in
[`jobs.toml`](../../src/session_ops/jobs.toml) whose stamp is older than its
`max_age_hours`, repeating daily while it stays quiet. A job with no stamp is timed from
the first check that found it missing, so installing the checker alerts on nothing.

A job in `jobs.toml` with a `schedule` is watched; the `session-ops@` template stamps it.
