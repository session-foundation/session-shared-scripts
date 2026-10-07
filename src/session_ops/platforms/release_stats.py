"""
Download counts of the last ten Desktop and Android releases, per installer, and the
latest Desktop release's downloads per platform, as CSV files. On demand only: nothing
schedules it.

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
FLATHUB = "https://flathub.org/api/v2/stats/network.loki.Session"
RELEASES = 10

DESKTOP_HEADER = ("version,snapshot_date,release_date,.deb,.appimage,.rpm,.dmg_arm64,"
                  ".dmg_x64,.zip_arm64,.zip_x64,.exe")
ANDROID_HEADER = ("version,snapshot_date,release_date,.aab,.apk_arm64,.apk_armv7a,"
                  ".apk_universal_huawei,.apk_universal_play,.apk_x86,.apk_x86_64")
PLATFORMS_HEADER = ("version,snapshot_date,release_date,linux,macos,windows,linux_github,"
                    "linux_flathub")

PLATFORM_EXTENSIONS = {
    "linux": (".deb", ".AppImage", ".rpm", ".freebsd"),
    "macos": (".dmg", ".zip"),
    "windows": (".exe",),
}


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


def latest(releases):
    return next(r for r in releases if not r["draft"] and not r["prerelease"])


def flathub_installs_since(stats, day):
    """Flathub installs from `day` on. Flathub builds from the GitHub .deb once, on its
    own servers, so these are not already in the GitHub counts."""
    per_day = stats["installs_per_day"]
    if day < min(per_day):
        raise RuntimeError(f"Flathub keeps daily installs from {min(per_day)} only, "
                           f"after the release on {day}")
    return sum(count for d, count in per_day.items() if d >= day)


def platform_totals(release, flathub_installs):
    totals = {platform: sum(a["download_count"] for a in release["assets"]
                            if a["name"].endswith(extensions))
              for platform, extensions in PLATFORM_EXTENSIONS.items()}
    totals["linux_github"] = totals["linux"]
    totals["linux_flathub"] = flathub_installs
    totals["linux"] += flathub_installs
    return totals


def platforms_csv(release, totals, snapshot):
    columns = PLATFORMS_HEADER.split(",")[3:]
    return "\n".join([PLATFORMS_HEADER, ",".join([
        release["tag_name"].removeprefix("v"), snapshot, release_day(release),
        *(str(totals[c]) for c in columns)])])


def release_day(release):
    return release["published_at"].split("T")[0]


def get_json(session, url):
    resp = session.request("GET", url)
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
    return resp.json()


def fetch(session, repo):
    return get_json(session, API.format(repo=repo))


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

    def emit(name, content):
        print(f"# {name}\n{content}\n")
        if not args.dry_run:
            os.makedirs(out, exist_ok=True)
            with open(os.path.join(out, f"{name}.csv"), "w", encoding="utf-8") as handle:
                handle.write(content)

    releases = {}
    for repo, header, row in (("session-desktop", DESKTOP_HEADER, desktop_row),
                              ("session-android", ANDROID_HEADER, android_row)):
        step(repo)
        releases[repo] = fetch(session, repo)
        emit(f"{repo}-release-stats", csv(releases[repo], header, row, snapshot))
    step("flathub")
    desktop = latest(releases["session-desktop"])
    flathub = flathub_installs_since(get_json(session, FLATHUB), release_day(desktop))
    emit("desktop-platform-totals",
         platforms_csv(desktop, platform_totals(desktop, flathub), snapshot))
    if not args.dry_run:
        print(f"Written to {out}")


if __name__ == "__main__":
    main()
