# Monthly Active Users

`/mau` in Discord answers with last month's monthly active users, per platform and in
total, or tells the person asking which exports it still lacks.

- Android: Play's MAU on the month's last day: users who opened Session in the 28 days
  before.
- Android outside Play: the latest stable release's GitHub APK downloads, and an F-Droid
  estimate of 10% of every other figure, since F-Droid publishes no counts.
- iOS: App Store Connect's Active in Last 30 Days on the month's last day, which counts
  only devices sharing analytics with developers, divided by the month's opt-in rate
  from Apple's App Opt In report, the share of first-time downloaders who opt in.
- Neither store has an API for the active users, so they come from the exports handed
  to `/mau-upload`. Each comes with the change on the month before.
- Desktop, which has no active-user count: the latest stable release's downloads per
  platform since its release, read from GitHub and Flathub when `/mau` runs.
- The total adds them all. The answer names every estimate and how it was made.

| | |
| --- | --- |
| Runs | `/mau [month:YYYY-MM]` in [Discord](session-ops-discord.md), and nothing else: no timer, so the silence checker does not watch it |
| Answers | The figures in the channel, through the interaction; anything else, a missing export or a failure, only to the person asking. A failure also goes to `alerts.env`'s channel |
| Secrets | `/etc/session-ops/mau.env`: `ASC_ISSUER_ID` and `ASC_KEY_ID`; `/etc/session-ops/asc-key.p8`, an App Store Connect team key with the Sales and Reports role |
| Inbox | `/var/lib/session-ops/mau/inbox/` |
| Dry run | `session-ops run mau --dry-run [-- --month YYYY-MM]` prints the answer and who would see it, and moves and writes nothing. On the host, see below |
| Logs | `journalctl -u session-ops@mau -n 50 --no-pager` |

## The exports

One file per platform, each a daily series covering the month's last day, from a store
set to English:

| | Where | What |
| --- | --- | --- |
| Android | Play Console → Statistics, a saved report | Monthly active users (MAU), Unique users, Per interval, Daily; all countries, no breakdown; Export report → CSV |
| iOS | App Store Connect → Analytics → Session → Metrics | Active in Last 30 Days, daily, no breakdown; Export |

Hand each to `/mau-upload`, which says at once if it is not an export the job reads,
and whether it has a final figure for last month's end. The job tells the two apart by
their columns, so both go in the same inbox. From a shell instead:

```sh
rsync "All countries _ regions.csv" session_private_messenger-active_last_30_days-*.csv \
    root@<host>:/var/lib/session-ops/mau/inbox/
```

`rsync`, not `scp`: it writes to a hidden temporary name and renames it once complete,
and the job only reads `*.csv`. App Store Connect's Active Devices export is refused:
it counts each day apart, and the App Store Connect API's App Sessions report is no
substitute, since summing its rows counts a device once per app version, OS and
territory it used in the month, 5% to 20% too many.

## What `/mau` does

The relay leaves the interaction to answer in `inbox/request.json` and starts the job,
which removes that file before anything else.

1. Merges every export in `inbox/` into `history.json`, one figure per platform and day,
   then moves it to `done/`. An export may cover any range; where two give a day different
   figures, the later one wins, and the change is kept until the figures are shown.
2. Moves a file it cannot read to `rejected/` and fails the run naming it, once it has
   answered.
3. Answers in the channel once `history.json` holds a final figure for the month's last
   day on both platforms. Play's figures trail by about eight days, so that is around the
   9th. Apple gives a day it is still counting as a fraction, which does not count as
   final. Until then, it tells the person asking which exports are missing.
4. Lists in the answer any change to the month before's last day, the figure the last
   answer showed, then forgets the changes. Asked again, it answers again.

A run that fails before step 1 leaves `inbox/` as it is; after it, the exports are in
`history.json`. Either way, the next `/mau` picks up from there.

The dry run on the host needs mau's account, environment and key, as its unit has them:

```bash
systemd-run --pipe --wait -p User=sessionops \
  -p EnvironmentFile=/etc/session-ops/alerts.env -p EnvironmentFile=/etc/session-ops/mau.env \
  -p LoadCredential=asc-key.p8:/etc/session-ops/asc-key.p8 \
  -p StateDirectory=session-ops/mau -p StateDirectoryMode=0711 -p UMask=0077 \
  /opt/session-ops/.venv/bin/session-ops run mau --dry-run -- --month 2026-09
```

Outside `--dry-run`, mau refuses to start as another account than the state's owner,
and does nothing without a request from `/mau`.

`history.json` cannot be rebuilt by a re-run: an unreadable one stops the job. If it is
lost, hand over an export covering the last 365 days for each platform.

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
publishes no download counts, so its figure is 10% of every other figure in the answer.
