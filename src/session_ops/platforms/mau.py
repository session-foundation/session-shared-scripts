"""
Monthly active users, posted once a month: Android's and iOS's from the store exports
dropped into the job's inbox, iOS's scaled up by Apple's opt-in rate from the App Store
Connect API, the latest Android APKs' and Desktop release's downloads from GitHub, and
an F-Droid estimate.

    session-ops run mau [--dry-run]
    /mau-upload file:<export>.csv        # in Discord, through session-ops-discord
    rsync <export>.csv root@<host>:/var/lib/session-ops/mau/inbox/

Every export's daily figures merge into history.json, so an export may cover any range
and overlap the previous one. A month is posted once its last day is in the history for
every platform; until then, from the REMIND_DAY on, each run posts a reminder instead.
"""
import argparse
import csv
import glob
import gzip
import io
import json
import os
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import jwt

from session_ops.ops.runner import step
from session_ops.shared import discord, http

PLAY_COLUMN = ("Monthly Active Users (MAU) (Unique users, Per interval, Daily): "
               "All countries / regions")
PLAY_DATE = "%b %d, %Y"
APPLE_COLUMN = "Active Last 30 Days"
APPLE_DATE = "%m/%d/%y"
PLATFORMS = ("android", "ios")
# Play's daily figures trail by about eight days, so a month's last day lands around the 9th.
REMIND_DAY = 10
ZONE = ZoneInfo("Australia/Melbourne")
VERSION = 1
HISTORY = "history.json"

ANDROID_RELEASES = "https://api.github.com/repos/session-foundation/session-android/releases"
DESKTOP_RELEASES = "https://api.github.com/repos/session-foundation/session-desktop/releases"
FLATHUB = "https://flathub.org/api/v2/stats/network.loki.Session"
PLATFORM_EXTENSIONS = {
    "linux": (".deb", ".AppImage", ".rpm", ".freebsd"),
    "macos": (".dmg", ".zip"),
    "windows": (".exe",),
}
# No store publishes F-Droid's downloads: this share of every other figure stands in for them.
FDROID_SHARE = 0.10

ASC_API = "https://api.appstoreconnect.apple.com"
IOS_APP_ID = "1470168868"
ASC_KEY_CREDENTIAL = "asc-key.p8"


class Rejected(ValueError):
    pass


def read_rows(path):
    try:
        with open(path, encoding="utf-8-sig", newline="") as handle:
            # strict: a quoted figure cut off at the end of the file is an error, not a smaller figure.
            return list(csv.reader(handle, strict=True))
    except (UnicodeDecodeError, csv.Error) as exc:
        raise Rejected(f"not a CSV export ({exc})") from None


def parse_export(path):
    """(platform, {ISO date: figure}) from a Play Console MAU export or an App Store
    Connect Active in Last 30 Days export."""
    rows = read_rows(path)
    first = rows[0] if rows and rows[0] else [""]
    if first[0] == "Date" and PLAY_COLUMN in first:
        return "android", parse_play(rows)
    if first[0] == "Name":
        return "ios", parse_apple(rows)
    raise Rejected("neither a Play Console MAU export nor an App Store Connect Active in "
                   "Last 30 Days one")


def parse_play(rows):
    column = rows[0].index(PLAY_COLUMN)
    days = {}
    for line, row in enumerate(rows[1:], start=2):
        try:
            day = datetime.strptime(row[0], PLAY_DATE).date().isoformat()
            figure = row[column].replace(",", "")
        except (IndexError, ValueError):
            raise Rejected(f"line {line} is not a date and a figure: {row[:column + 1]}") from None
        if not figure:
            continue
        if not figure.isdigit():
            raise Rejected(f"line {line} has {row[column]!r} for a figure")
        days[day] = int(figure)
    return non_empty(days)


def parse_apple(rows):
    """The table below App Store Connect's Name, Start Date and End Date lines."""
    try:
        header = next(i for i, row in enumerate(rows) if row[:1] == ["Date"])
    except StopIteration:
        raise Rejected("an App Store Connect export with no Date column") from None
    if rows[header] != ["Date", APPLE_COLUMN]:
        raise Rejected(f"an App Store Connect export of {', '.join(rows[header][1:])}: "
                       f"export {APPLE_COLUMN} instead, daily")
    days = {}
    for line, row in enumerate(rows[header + 1:], start=header + 2):
        try:
            day = datetime.strptime(row[0], APPLE_DATE).date().isoformat()
            # A day Apple is still counting comes as a fraction; a later export revises it.
            value = round(float(row[1]))
        except (IndexError, ValueError, OverflowError):
            raise Rejected(f"line {line} is not a date and a figure: {row[:2]}") from None
        if value < 0:
            raise Rejected(f"line {line} has {row[1]!r} for a figure")
        days[day] = value
    return non_empty(days)


def non_empty(days):
    if not days:
        raise Rejected("no figures in it")
    return days


def read_inbox(inbox):
    """(path, platform, days) for each export, oldest first, and (path, reason) for each
    rejected one.

    glob skips dotfiles, so rsync's temporary file is never read half-written.
    """
    exports, rejected = [], []
    for path in sorted(glob.glob(os.path.join(inbox, "*.csv")), key=os.path.getmtime):
        try:
            exports.append((path, *parse_export(path)))
        except Rejected as exc:
            rejected.append((path, str(exc)))
    return exports, rejected


def merge(history, exports):
    """Merge each export into its platform's history, the later export winning; returns
    the revisions, (platform, day, old, new), for the figures a later export changed."""
    revisions = []
    for _, platform, days in exports:
        for day, figure in sorted(days.items()):
            old = history[platform].get(day)
            if old is not None and old != figure:
                revisions.append((platform, day, old, figure))
            history[platform][day] = figure
    return revisions


def file_away(path, folder):
    os.makedirs(folder, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    os.replace(path, os.path.join(folder, f"{stamp}-{os.path.basename(path)}"))


def load_history(path):
    """The history at `path`, empty if there is none yet. Unlike a digest's dedup cache it
    cannot be rebuilt from a re-run, so an unreadable one stops the run rather than reset."""
    if not os.path.exists(path):
        return {"version": VERSION, "android": {}, "ios": {}, "posted": []}
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    if data.get("version") != VERSION:
        raise RuntimeError(f"{path} is version {data.get('version')!r}, expected {VERSION}")
    return data


def save_history(path, history):
    temporary = f"{path}.tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(history, handle, indent=2, sort_keys=True)
    os.replace(temporary, path)


def previous_month_end(today):
    return today.replace(day=1) - timedelta(days=1)


def figure(value):
    return f"{value:,}"


def get_json(session, url):
    resp = session.request("GET", url)
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
    return resp.json()


def latest(releases):
    return next(r for r in releases if not r["draft"] and not r["prerelease"])


def release_day(release):
    return release["published_at"].split("T")[0]


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
    totals["linux"] += flathub_installs
    return totals


def desktop_downloads(session):
    """(latest stable Desktop release, its downloads per platform)."""
    release = latest(get_json(session, DESKTOP_RELEASES))
    flathub = flathub_installs_since(get_json(session, FLATHUB), release_day(release))
    return release, platform_totals(release, flathub)


def apk_downloads(session):
    """(latest stable Android release, its APKs' downloads)."""
    release = latest(get_json(session, ANDROID_RELEASES))
    return release, sum(a["download_count"] for a in release["assets"]
                        if a["name"].endswith(".apk"))


def asc_token(issuer, key_id, private_key):
    now = int(time.time())
    return jwt.encode({"iss": issuer, "iat": now, "exp": now + 1200, "aud": "appstoreconnect-v1"},
                      private_key, algorithm="ES256", headers={"kid": key_id, "typ": "JWT"})


def asc_list(session, path):
    url = ASC_API + path
    while url:
        page = get_json(session, url)
        yield from page["data"]
        url = page.get("links", {}).get("next")


def opt_in_days(asc, downloads, since):
    """{ISO date: (first-time downloaders, those opting in)} from the App Opt In report of
    every analytics request on the app, processed on or after `since`; where instances
    overlap, the latest processed wins, since a later one carries late events.

    `downloads` fetches the segments: their URLs are pre-signed, so take no API token.
    """
    requests = list(asc_list(asc, f"/v1/apps/{IOS_APP_ID}/analyticsReportRequests"))
    stopped = [r["id"] for r in requests if r["attributes"]["accessType"] == "ONGOING"
               and r["attributes"]["stoppedDueToInactivity"]]
    if stopped:
        raise RuntimeError(f"Apple stopped the ongoing analytics request {stopped[0]} for "
                           "inactivity; an Admin key must create a new one")
    instances = []
    for request in requests:
        for report in asc_list(asc, f"/v1/analyticsReportRequests/{request['id']}/reports"
                                    "?filter[name]=App%20Opt%20In"):
            instances += [i for i in asc_list(asc, f"/v1/analyticsReports/{report['id']}/instances"
                                                   "?filter[granularity]=DAILY&limit=200")
                          if i["attributes"]["processingDate"] >= since]
    days = {}
    for instance in sorted(instances, key=lambda i: i["attributes"]["processingDate"]):
        for segment in asc_list(asc, f"/v1/analyticsReportInstances/{instance['id']}/segments"):
            resp = downloads.request("GET", segment["attributes"]["url"])
            if resp.status_code != 200:
                raise RuntimeError(f"HTTP {resp.status_code} for an App Opt In segment")
            text = gzip.decompress(resp.content).decode("utf-8")
            for row in csv.DictReader(io.StringIO(text), delimiter="\t"):
                days[row["Date"]] = (int(row["Downloading Users"] or 0),
                                     int(row["Users Opting-In"] or 0))
    return days


def opt_in(days, month_end):
    """(first-time downloaders, those opting in) over the month, or None without any."""
    month = [v for d, v in days.items() if d[:7] == month_end.isoformat()[:7]]
    downloading = sum(v[0] for v in month)
    return (downloading, sum(v[1] for v in month)) if downloading else None


def ios_estimate(opted_in, opt_in_counts):
    downloading, opting_in = opt_in_counts
    return round(opted_in * downloading / opting_in)


LABELS = {"android": "Android", "ios": "iOS"}
HOW_TO_EXPORT = {
    "android": "Play Console → Statistics → Saved reports → the MAU report, covering {day} → "
               "Export report → CSV",
    "ios": "App Store Connect → Analytics → Session → Metrics → Active in Last 30 Days, daily, "
           "covering {day} → Export",
}


def with_change(current, before, month_end):
    text = f"**{figure(current)}**"
    if before:
        change = current - before
        text += (f" ({'+' if change >= 0 else '−'}{figure(abs(change))}, "
                 f"{change / before:+.1%} on {previous_month_end(month_end):%B})")
    return text


def report_message(month_end, history, revisions, sources):
    """`sources`: the desktop and APK (release, downloads) pairs, and Apple's opt-in
    counts for this month and the one before, None where it has no first-time downloader."""
    day, before_day = month_end.isoformat(), previous_month_end(month_end).isoformat()
    desktop_release, downloads = sources["desktop"]
    apk_release, apks = sources["apks"]
    counts, before_counts = sources["opt_in"]
    if not counts or not counts[1]:
        raise RuntimeError(f"Apple has no opt-in rate for {month_end:%B %Y}")
    play = history["android"][day]
    opted_in = history["ios"][day]
    ios = ios_estimate(opted_in, counts)
    ios_before = (ios_estimate(history["ios"][before_day], before_counts)
                  if before_counts and before_counts[1] and before_day in history["ios"]
                  else None)
    desktop = downloads["linux"] + downloads["macos"] + downloads["windows"]
    fdroid = round(FDROID_SHARE * (play + ios + apks + desktop))
    total = play + ios + apks + fdroid + desktop
    on = f"{month_end:%-d %B}"
    lines = [
        f"📊 **Monthly active users, {month_end:%B %Y}**",
        f"Android, Play: {with_change(play, history['android'].get(before_day), month_end)}",
        f"Android, outside Play: **{figure(apks + fdroid)}** "
        f"(GitHub APKs {figure(apks)} · F-Droid {figure(fdroid)}, estimated)",
        f"iOS: {with_change(ios, ios_before, month_end)}, estimated",
        f"Desktop: **{figure(desktop)}** (Linux {figure(downloads['linux'])} · "
        f"macOS {figure(downloads['macos'])} · Windows {figure(downloads['windows'])})",
        f"**Total: {figure(total)}**",
        f"-# Android, Play: Play Console MAU on {on}, users who opened Session in the 28 days "
        "before.",
        f"-# GitHub APKs: downloads of {apk_release['tag_name']}'s APKs since its release on "
        f"{date.fromisoformat(release_day(apk_release)):%-d %B}, updates included.",
        f"-# F-Droid publishes no counts: estimated as {FDROID_SHARE:.0%} of the other figures.",
        f"-# iOS: Apple counts only devices that share analytics, {figure(opted_in)} active in "
        f"the 30 days to {on}, scaled by {month_end:%B}'s opt-in rate from Apple's App Opt In "
        f"report: {counts[1] / counts[0]:.1%}, {figure(counts[1])} of {figure(counts[0])} "
        "first-time downloaders.",
        f"-# Desktop: downloads of {desktop_release['tag_name']} since its release on "
        f"{date.fromisoformat(release_day(desktop_release)):%-d %B}, updates included; "
        "Desktop has no active-user count.",
    ]
    if revisions:
        lines.append("-# The latest exports revised " + ", ".join(
            f"{LABELS[p]} {date.fromisoformat(d):%-d %b} {figure(old)} → {figure(new)}"
            for p, d, old, new in revisions))
    return "\n".join(lines)


def reminder_message(month_end, missing):
    names = " and ".join(LABELS[p] for p in missing)
    day = f"{month_end:%-d %B}"
    return "\n".join([
        f"⏰ **{names} active users for {month_end:%B %Y} {'are' if len(missing) > 1 else 'is'} "
        "missing.** Export:",
        *(f"- {LABELS[p]}: " + HOW_TO_EXPORT[p].format(day=day) for p in missing),
        "Then hand each to `/mau-upload` here, and the figures post as soon as the last one "
        "lands.",
    ])


def read_sources(args, month_end):
    step("reading GitHub and Flathub downloads")
    github = http.Session()
    github.headers.update({"Accept": "application/vnd.github.v3+json"})
    sources = {"desktop": desktop_downloads(github), "apks": apk_downloads(github)}

    step("reading Apple's opt-in rate")
    with open(args.asc_key, encoding="utf-8") as handle:
        token = asc_token(os.environ["ASC_ISSUER_ID"], os.environ["ASC_KEY_ID"], handle.read())
    asc = http.Session()
    asc.headers.update({"Authorization": f"Bearer {token}"})
    month_before = previous_month_end(month_end)
    days = opt_in_days(asc, http.Session(), since=month_before.replace(day=1).isoformat())
    sources["opt_in"] = (opt_in(days, month_end), opt_in(days, month_before))
    return sources


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.strip().split("\n")[0])
    parser.add_argument("--state", required=True, metavar="DIR",
                        help="Holds inbox/, done/, rejected/ and history.json.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would be posted; move and write nothing.")
    parser.add_argument("--asc-key", metavar="PATH",
                        default=os.path.join(os.environ.get("CREDENTIALS_DIRECTORY", ""),
                                             ASC_KEY_CREDENTIAL),
                        help="The App Store Connect API key, for Apple's opt-in rate.")
    args = parser.parse_args(argv)

    inbox = os.path.join(args.state, "inbox")
    history_path = os.path.join(args.state, HISTORY)
    step("reading the inbox")
    history = load_history(history_path)
    exports, rejected = read_inbox(inbox)
    revisions = merge(history, exports)
    for platform, day, old, new in revisions:
        print(f"{platform} {day}: {old} revised to {new}")
    if not args.dry_run:
        # Saved before the files move: a run stopped in between reads them again, harmlessly.
        save_history(history_path, history)
        for path, _, _ in exports:
            file_away(path, os.path.join(args.state, "done"))
        for path, _ in rejected:
            file_away(path, os.path.join(args.state, "rejected"))

    today = datetime.now(ZONE).date()
    month_end = previous_month_end(today)
    month = month_end.strftime("%Y-%m")
    missing = [p for p in PLATFORMS if month_end.isoformat() not in history[p]]
    message = None
    if month in history["posted"]:
        print(f"{month} already posted.")
    elif not missing:
        message = report_message(month_end, history, revisions, read_sources(args, month_end))
    elif today.day >= REMIND_DAY:
        message = reminder_message(month_end, missing)
    else:
        print(f"Waiting for {month_end} from {', '.join(missing)}; "
              f"reminders start on the {REMIND_DAY}th.")

    if message:
        step("posting to Discord")
        if args.dry_run:
            print(message)
        else:
            payload = {"content": message, "allowed_mentions": {"parse": []}}
            if not discord.post_to_discord(http.Session(), os.environ["MAU_DISCORD_WEBHOOK_URL"],
                                           [payload]):
                raise RuntimeError("Discord did not accept the message")
            if not missing:
                history["posted"].append(month)
                save_history(history_path, history)
    if rejected:
        raise SystemExit("rejected " + "; ".join(
            f"{os.path.basename(path)}: {reason}" for path, reason in rejected))


if __name__ == "__main__":
    main()
