# Monthly Active Users

Posts last month's monthly active users once a month, per platform and in total.

- Android: Play's MAU on the month's last day, with the change on the month before.
  Google has no API for it, so the figures come from the Play Console export someone
  drops in the job's inbox.
- Desktop, which has no active-user count: the latest stable release's downloads per
  platform since its release, read from GitHub and Flathub when the post goes out.
- The total adds the two.

| | |
| --- | --- |
| Runs | `session-ops@mau.timer` on the 10th at 11:00 Melbourne, and `session-ops@mau.path` whenever a `.csv` lands in the inbox |
| Secrets | `/etc/session-ops/mau.env`: `MAU_DISCORD_WEBHOOK_URL`, and `MAU_INBOX_HOST` for the reminder's `rsync` |
| Inbox | `/var/lib/session-ops/mau/inbox/` |
| Dry run | `session-ops run mau --dry-run` prints what it would post, and moves and writes nothing |
| Logs | `journalctl -u session-ops@mau -n 50 --no-pager` |

## The export

In Play Console, Statistics, a report saved once:

- Metric: Monthly active users (MAU), Unique users, Per interval, Daily
- All countries / regions, no breakdown
- A date range ending today, such as Last 30 days, with the Console in English

Export report → CSV, then:

```sh
rsync "All countries _ regions.csv" root@<host>:/var/lib/session-ops/mau/inbox/
```

`rsync`, not `scp`: it writes to a hidden temporary name and renames it once complete,
and the job only reads `*.csv`.

## What a run does

1. Merges every export in `inbox/` into `history.json`, one figure per day, then moves
   it to `done/`. An export may cover any range; where two give a day different
   figures, the later one wins and the post lists the revision.
2. Moves a file it cannot read to `rejected/` and fails the run naming it, after the
   rest of the run.
3. Posts last month once `history.json` holds its last day. Play's figures trail by
   about eight days, so that is around the 9th. Until then, from the 10th, each run
   posts a reminder instead.
4. Records the month as posted, so neither the timer nor a later export posts it again.

`history.json` cannot be rebuilt by a re-run: an unreadable one stops the job. If it is
lost, drop an export covering the last 365 days.

## Desktop downloads

The latest Desktop release that is neither a draft nor a pre-release. Updates count
where the updater fetches an installer.

| Platform | Counted |
| --- | --- |
| Linux | `.deb`, `.AppImage`, `.rpm`, `.freebsd`, and Flathub's installs of `network.loki.Session` since the release day |
| macOS | `.dmg`, `.zip` |
| Windows | `.exe` |

`.blockmap`, `latest*.yml` and `signature.asc` are left out. Flathub builds from the
GitHub `.deb` once, so its installs are not in GitHub's counts; Homebrew's `session`
cask downloads the GitHub `.dmg`, so it already is. Flathub keeps 180 days of daily
installs, so the run fails on a release older than that rather than undercount.
