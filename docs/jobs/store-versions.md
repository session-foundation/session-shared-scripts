# Store Versions Against GitHub Releases

Every 5 minutes, compares each store's latest Session release with the platform's
highest GitHub release, and posts as they diverge and come back.

| | |
| --- | --- |
| Runs | `session-ops@store-versions.timer`, every 5 minutes |
| Secrets | `/etc/session-ops/store-versions.env`: `STORE_VERSIONS_DISCORD_WEBHOOK_URL` |
| Tags | `/etc/session-ops/store-versions.toml`, from [`store-versions.toml.example`](../../deploy/env/store-versions.toml.example); without it nobody is tagged |
| Dry run | `session-ops run store-versions --dry-run` prints every version and what it would post. On the host, through `systemd-run` as in [deploy/README.md](../../deploy/README.md), so it gets the env file |
| On demand | [`/versions`](session-ops-discord.md) in Discord shows each platform's versions from the last run |
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
repo is published with the release. A store shipping a published prerelease above
that release is in sync with it.

| Post | When |
| --- | --- |
| ⚠️ warning, tagging the platform's people | it diverges; on a platform's first check, if it already has |
| ⚠️ again | every 12 hours while unresolved, and at once if the gap grows |
| ✅ back in sync, or no longer ahead | after a warning only |
| ℹ️ new GitHub release, store updated | either moves without a warning |
| ⚠️ source failing, untagged | a source has failed for an hour, then every 12 hours, and ✅ once it reads again |

A store is held at the highest version it has served: iTunes and the CDNs re-serve the
previous one for hours after a release, so a store returning to it changes nothing.
Once a store has served a lower version for 6 hours, that version counts, with nothing
to edit by hand: a bad read clears itself, a warning out is updated to the new gap, and
a rollback warns on the channels warned on either way.

State is `/var/lib/session-ops/store-versions/state.json`. Losing it makes the next run
a first check. A message Discord refuses is posted again on the next run, as is one
still unposted 100 seconds into a run. Every request has a wall-clock limit, 60 s for a
source and 20 s for a post, so a run and its failure alert end inside the 4-minute
timeout. A version that does not start with a digit, or runs past 64 characters, fails
its source. A failed run is reported once, then every 12 hours until one succeeds.

GitHub is read unauthenticated: one request per repository a run, 36 an hour of the 60
allowed. The organisation refuses fine-grained tokens that live longer than 366 days.
