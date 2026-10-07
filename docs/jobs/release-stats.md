# Release Download Statistics

Download counts of the last ten Desktop and Android releases, per installer, from the
public releases API, and the latest Desktop release's downloads per platform, as three
CSV files.

| | |
| --- | --- |
| Runs | on demand only: `systemctl start session-ops@release-stats.service` |
| Secrets | none; `/etc/session-ops/alerts.env` for its failures |
| Dry run | `session-ops run release-stats --dry-run` prints the CSVs and writes nothing |
| Logs | `journalctl -u session-ops@release-stats -n 50 --no-pager`, which carries every CSV |

The files land in `/var/lib/session-ops/release-stats/runs/<stamp>/` and are pruned
after 14 days. From a checkout, `uv run python -m session_ops.platforms.release_stats
--out .` writes them to a timestamped folder in the current directory, `./<YYYYmmddTHHMMSSZ>/`.

| Desktop column | Assets counted |
| --- | --- |
| `.deb`, `.appimage`, `.rpm`, `.exe` | by extension |
| `.dmg_arm64`, `.dmg_x64`, `.zip_arm64`, `.zip_x64` | by extension and architecture |

| Android column | Assets counted |
| --- | --- |
| `.aab` | `play-release` bundles |
| `.apk_arm64`, `.apk_armv7a`, `.apk_x86_64` | `play-release` APKs for `arm64-v8a`, `armeabi-v7a`, `x86_64` |
| `.apk_x86` | `play-release` x86 APKs, not x86_64 |
| `.apk_universal_play`, `.apk_universal_huawei` | universal APKs per store |

`desktop-platform-totals.csv` holds one row: the downloads of the latest Desktop
release, drafts and pre-releases aside, per platform, from its publication to the
snapshot. Updates count as downloads where the updater fetches an installer.

| Column | Counted |
| --- | --- |
| `linux` | `linux_github` + `linux_flathub` |
| `linux_github` | `.deb`, `.AppImage`, `.rpm`, `.freebsd` |
| `linux_flathub` | installs of `network.loki.Session` from Flathub's stats API since the release day |
| `macos` | `.dmg`, `.zip` |
| `windows` | `.exe` |

`.blockmap`, `latest*.yml` and `signature.asc` are left out. Homebrew's `session` cask
downloads the GitHub `.dmg`, so it is already in `macos`. Flathub keeps 180 days of
daily installs, so the run fails on a release older than that rather than undercount.
