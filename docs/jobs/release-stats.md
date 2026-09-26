# Release Download Statistics

Download counts of the last ten Desktop and Android releases, per installer, from the
public releases API, as two CSV files.

| | |
| --- | --- |
| Runs | on demand only: `systemctl start session-ops@release-stats.service` |
| Secrets | none; `/etc/session-ops/alerts.env` for its failures |
| Dry run | `session-ops run release-stats --dry-run` prints the CSVs and writes nothing |
| Logs | `journalctl -u session-ops@release-stats -n 50 --no-pager`, which carries both CSVs |

The files land in `/var/lib/session-ops/release-stats/runs/<stamp>/` and are pruned
after 14 days. From a checkout, `uv run python -m session_ops.platforms.release_stats
--out .` writes them to the current directory.

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
