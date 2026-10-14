# Store Versions Against GitHub Releases

Every 5 minutes, compares each store's latest Session release with the platform's latest
GitHub release, and posts as they diverge and come back.

| | |
| --- | --- |
| Runs | `session-ops@store-versions.timer`, every 5 minutes |
| Secrets | `/etc/session-ops/store-versions.env`: `STORE_VERSIONS_DISCORD_WEBHOOK_URL` |
| Tags | `/etc/session-ops/store-versions.toml`, from [`store-versions.toml.example`](../../deploy/env/store-versions.toml.example); without it nobody is tagged |
| Dry run | `session-ops run store-versions --dry-run` prints every version and what it would post |
| Logs | `journalctl -u session-ops@store-versions -n 50 --no-pager` |

| Platform | GitHub | Store | Warns when the store is |
| --- | --- | --- | --- |
| `android` | `session-android` | Play listing, scraped: Play has no version API | ahead |
| `android-fdroid-merged` | `session-android` | `index-v1.json` on session-fdroid's `master` | ahead or behind |
| `android-fdroid-live` | `session-android` | `fdroid.getsession.org`'s `index-v1.json` | ahead |
| `ios` | `session-ios` | iTunes lookup API | ahead |
| `desktop` | `session-desktop` | `deb.session.foundation`, `sid`, package `session-desktop` | ahead or behind |
| `desktop-flathub-merged` | `session-desktop` | `network.loki.Session.metainfo.xml` on flathub/network.loki.Session's `master` | ahead or behind |
| `desktop-flathub-live` | `session-desktop` | Flathub's appstream API for `network.loki.Session` | ahead |

A store ahead of GitHub is a public build whose source is not published. The channels
warned on either way are ours: each GitHub release opens a pull request on
session-fdroid and on flathub/network.loki.Session that has to be merged, and the APT
repo is published with the release.

| Post | When |
| --- | --- |
| ⚠️ warning, tagging the platform's people | it diverges; on a platform's first check, if it already has |
| ⚠️ again | every 12 hours while unresolved, and at once if the gap grows |
| ✅ back in sync | after a warning only |
| ℹ️ new GitHub release, store updated | either moves without a warning; a store returning to a version it already reached stays quiet, since iTunes and Flathub's CDN re-serve the previous one for hours |
| ⚠️ source failing, untagged | a source has failed for an hour, then every 12 hours, and ✅ once it reads again |

State is `/var/lib/session-ops/store-versions/state.json`. Losing it makes the next run
a first check. A message Discord refuses is posted again on the next run.

GitHub is read unauthenticated: one request per repository a run, 36 an hour of the 60
allowed. The organisation refuses fine-grained tokens that live longer than 366 days.
