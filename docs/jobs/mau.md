# Monthly Active Users

Posts last month's monthly active users once a month, per platform and in total.

- Android: Play's MAU on the month's last day: users who opened Session in the 28 days
  before.
- Android outside Play: the latest stable release's GitHub APK downloads, and an F-Droid
  estimate of 10% of every other figure, since F-Droid publishes no counts.
- iOS: App Store Connect's Active in Last 30 Days on the month's last day, which counts
  only devices sharing analytics with developers, divided by the month's opt-in rate
  from Apple's App Opt In report, the share of first-time downloaders who opt in.
- Neither store has an API for the active users, so they come from the exports someone
  drops in the job's inbox. Each comes with the change on the month before.
- Desktop, which has no active-user count: the latest stable release's downloads per
  platform since its release, read from GitHub and Flathub when the post goes out.
- The total adds them all. The post names every estimate and how it was made.

| | |
| --- | --- |
| Runs | `session-ops@mau.timer` on the 10th at 11:00 Melbourne, and `session-ops@mau.path` whenever a `.csv` lands in the inbox |
| Secrets | `/etc/session-ops/mau.env`: `MAU_DISCORD_WEBHOOK_URL`, `ASC_ISSUER_ID` and `ASC_KEY_ID`; `/etc/session-ops/asc-key.p8`, an App Store Connect team key with the Sales and Reports role |
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

Hand each to `/mau-upload` in Discord, which says at once if it is not an export the
job reads. The job tells the two apart by their columns, so both go in the same inbox.
From a shell instead:

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
   figures, the later one wins, and the change is kept until the month posts.
2. Moves a file it cannot read to `rejected/` and fails the run naming it, after the
   rest of the run.
3. Posts last month once `history.json` holds a final figure for its last day on both
   platforms. Play's figures trail by about eight days, so that is around the 9th. Apple
   gives a day it is still counting as a fraction, which does not count as final. Until
   then, from the 10th, each run posts a reminder naming the platforms still missing.
4. Lists in the post the changes to the two month ends it compares, records the month as
   posted, so neither the timer nor a later export posts it again, and forgets the
   changes.

Should a run fail before the inbox is filed, say on an unreadable `history.json`, it
moves every export in `inbox/` to `rejected/` first: the path unit starts the job again
for as long as a file matches, and gives up watching after five starts. Once the cause
is fixed, move them back into `inbox/`.

`history.json` cannot be rebuilt by a re-run: an unreadable one stops the job. If it is
lost, drop an export covering the last 365 days for each platform.

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

## Apple's opt-in rate

The job reads the App Opt In report from every analytics report request on Session's
App Store Connect app: the one-time snapshot for history, and the ongoing one for each
new day. A Sales and Reports key may read them but not create them; if Apple stops the
ongoing request for inactivity, the run fails saying so, and an Admin key has to
create a new one (`POST /v1/analyticsReportRequests`, `accessType: ONGOING`).

## Android outside Play

The `.apk` files of the latest Android release on GitHub that is neither a draft nor a
pre-release, since its release, updates included. F-Droid builds its own APK and
publishes no download counts, so its figure is 10% of every other figure in the post.
