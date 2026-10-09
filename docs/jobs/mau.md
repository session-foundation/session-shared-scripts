# Monthly Active Users

Posts last month's monthly active users once a month, per platform and in total.

- Android: Play's MAU on the month's last day: users who opened Session in the 28 days
  before.
- iOS: App Store Connect's Active in Last 30 Days on the month's last day: devices,
  counting only those that share analytics with developers.
- Neither store has an API for these figures, so they come from the exports someone
  drops in the job's inbox. Each comes with the change on the month before.
- Desktop, which has no active-user count: the latest stable release's downloads per
  platform since its release, read from GitHub and Flathub when the post goes out.
- The total adds all three.

| | |
| --- | --- |
| Runs | `session-ops@mau.timer` on the 10th at 11:00 Melbourne, and `session-ops@mau.path` whenever a `.csv` lands in the inbox |
| Secrets | `/etc/session-ops/mau.env`: `MAU_DISCORD_WEBHOOK_URL`, and `MAU_INBOX_HOST` for the reminder's `rsync` |
| Inbox | `/var/lib/session-ops/mau/inbox/` |
| Dry run | `session-ops run mau --dry-run` prints what it would post, and moves and writes nothing |
| Logs | `journalctl -u session-ops@mau -n 50 --no-pager` |

## The exports

One file per platform, each a daily series covering the month's last day, from a store
set to English:

| | Where | What |
| --- | --- | --- |
| Android | Play Console → Statistics, a saved report | Monthly active users (MAU), Unique users, Per interval, Daily; all countries, no breakdown; Export report → CSV |
| iOS | App Store Connect → Analytics → Session → Metrics | Active in Last 30 Days, daily, no breakdown; Export |

The job tells them apart by their columns, so both go in the same inbox:

```sh
rsync "All countries _ regions.csv" session_private_messenger-active_last_30_days-*.csv \
    root@<host>:/var/lib/session-ops/mau/inbox/
```

`rsync`, not `scp`: it writes to a hidden temporary name and renames it once complete,
and the job only reads `*.csv`. App Store Connect's Active Devices export is refused:
it counts each day apart, and the App Store Connect API's App Sessions report is no
substitute, since summing its rows counts a device once per app version, OS and
territory it used in the month, 5% to 20% too many.

## What a run does

1. Merges every export in `inbox/` into `history.json`, one figure per platform and day,
   then moves it to `done/`. An export may cover any range; where two give a day different
   figures, the later one wins and the post lists the revision.
2. Moves a file it cannot read to `rejected/` and fails the run naming it, after the
   rest of the run.
3. Posts last month once `history.json` holds its last day for both platforms. Play's
   figures trail by about eight days, so that is around the 9th. Until then, from the
   10th, each run posts a reminder naming the platforms still missing.
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
