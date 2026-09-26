"""
Download counts of the last ten Desktop and Android releases, per installer, as two
CSV files. On demand only: nothing schedules it.

    session-ops run release-stats          # CSVs kept under the job's runs/
    uv run python -m session_ops.platforms.release_stats --out .
"""
import argparse
import os
import time
from datetime import datetime, timezone

from session_ops.ops.runner import step
from session_ops.shared import http

API = "https://api.github.com/repos/session-foundation/{repo}/releases"
RELEASES = 10

DESKTOP_HEADER = ("version,snapshot_date,release_date,.deb,.appimage,.rpm,.dmg_arm64,"
                  ".dmg_x64,.zip_arm64,.zip_x64,.exe")
ANDROID_HEADER = ("version,snapshot_date,release_date,.aab,.apk_arm64,.apk_armv7a,"
                  ".apk_universal_huawei,.apk_universal_play,.apk_x86,.apk_x86_64")


def downloads(assets, keep):
    return sum(a["download_count"] for a in assets if keep(a["name"]))


def desktop_row(release):
    assets = release["assets"]
    return [
        downloads(assets, lambda n: n.endswith(".deb")),
        downloads(assets, lambda n: n.endswith(".AppImage")),
        downloads(assets, lambda n: n.endswith(".rpm")),
        downloads(assets, lambda n: n.endswith(".dmg") and "arm64" in n),
        downloads(assets, lambda n: n.endswith(".dmg") and "x64" in n),
        downloads(assets, lambda n: n.endswith(".zip") and "arm64" in n),
        downloads(assets, lambda n: n.endswith(".zip") and "x64" in n),
        downloads(assets, lambda n: n.endswith(".exe")),
    ]


def android_row(release):
    assets = release["assets"]

    def apk(arch):
        return downloads(assets, lambda n: n.endswith(".apk") and arch in n
                         and "play-release" in n)

    return [
        downloads(assets, lambda n: n.endswith(".aab") and "play-release" in n),
        apk("arm64-v8a"),
        apk("armeabi-v7a"),
        downloads(assets, lambda n: n.endswith(".apk") and "universal" in n
                  and "huawei-release" in n),
        downloads(assets, lambda n: n.endswith(".apk") and "universal" in n
                  and "play-release" in n),
        # x86 without x86_64, which contains it.
        downloads(assets, lambda n: n.endswith(".apk") and "x86" in n
                  and "x86_64" not in n and "play-release" in n),
        apk("x86_64"),
    ]


def csv(releases, header, row, snapshot):
    lines = [header]
    for release in releases[:RELEASES]:
        version = release["tag_name"][1:] if release["tag_name"].startswith("v") \
            else release["tag_name"]
        lines.append(",".join(str(v) for v in [version, snapshot,
                                               release["published_at"].split("T")[0],
                                               *row(release)]))
    return "\n".join(lines)


def fetch(session, repo):
    resp = session.request("GET", API.format(repo=repo))
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
    return resp.json()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.strip().split("\n")[0])
    parser.add_argument("--out", default=".", metavar="DIR",
                        help="Where the CSVs go; a directory per run is made under it.")
    parser.add_argument("--dry-run", action="store_true", help="Print the CSVs only.")
    args = parser.parse_args(argv)

    session = http.Session()
    session.headers.update({"Accept": "application/vnd.github.v3+json"})
    snapshot = datetime.now(timezone.utc).date().isoformat()
    out = os.path.join(args.out, time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()))
    for repo, header, row in (("session-desktop", DESKTOP_HEADER, desktop_row),
                              ("session-android", ANDROID_HEADER, android_row)):
        step(repo)
        content = csv(fetch(session, repo), header, row, snapshot)
        print(f"# {repo}\n{content}\n")
        if not args.dry_run:
            os.makedirs(out, exist_ok=True)
            with open(os.path.join(out, f"{repo}-release-stats.csv"), "w",
                      encoding="utf-8") as handle:
                handle.write(content)
    if not args.dry_run:
        print(f"Written to {out}")


if __name__ == "__main__":
    main()
