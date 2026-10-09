"""
Monthly active users, posted once a month: Android's and iOS's from the store exports
dropped into the job's inbox, and the latest Desktop release's downloads, which Desktop
has in place of active users.

    session-ops run mau [--dry-run]
    rsync <export>.csv root@<host>:/var/lib/session-ops/mau/inbox/

Every export's daily figures merge into history.json, so an export may cover any range
and overlap the previous one. A month is posted once its last day is in the history for
every platform; until then, from the REMIND_DAY on, each run posts a reminder instead.
"""
import argparse
import csv
import glob
import json
import os
import socket
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

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

DESKTOP_RELEASES = "https://api.github.com/repos/session-foundation/session-desktop/releases"
FLATHUB = "https://flathub.org/api/v2/stats/network.loki.Session"
PLATFORM_EXTENSIONS = {
    "linux": (".deb", ".AppImage", ".rpm", ".freebsd"),
    "macos": (".dmg", ".zip"),
    "windows": (".exe",),
}


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


LABELS = {"android": "Android", "ios": "iOS"}
FOOTNOTES = {
    "android": "Play Console MAU on {day}, users who opened Session in the 28 days before.",
    "ios": "App Store Connect's devices active in the 30 days to {day}, counting only those "
           "that share analytics with developers.",
}
HOW_TO_EXPORT = {
    "android": "Play Console → Statistics → Saved reports → the MAU report, covering {day} → "
               "Export report → CSV",
    "ios": "App Store Connect → Analytics → Session → Metrics → Active in Last 30 Days, daily, "
           "covering {day} → Export",
}


def with_change(history, month_end):
    current = history[month_end.isoformat()]
    text = f"**{figure(current)}**"
    before = history.get(previous_month_end(month_end).isoformat())
    if before:
        change = current - before
        text += (f" ({'+' if change >= 0 else '−'}{figure(abs(change))}, "
                 f"{change / before:+.1%} on {previous_month_end(month_end):%B})")
    return text


def report_message(month_end, history, revisions, desktop):
    day = month_end.isoformat()
    release, downloads = desktop
    desktop_total = downloads["linux"] + downloads["macos"] + downloads["windows"]
    total = sum(history[p][day] for p in PLATFORMS) + desktop_total
    lines = [f"📊 **Monthly active users, {month_end:%B %Y}**"]
    lines += [f"{LABELS[p]}: {with_change(history[p], month_end)}" for p in PLATFORMS]
    lines += [
        f"Desktop: **{figure(desktop_total)}** (Linux {figure(downloads['linux'])} · "
        f"macOS {figure(downloads['macos'])} · Windows {figure(downloads['windows'])})",
        f"**Total: {figure(total)}**",
    ]
    lines += [f"-# {LABELS[p]}: " + FOOTNOTES[p].format(day=f"{month_end:%-d %B}")
              for p in PLATFORMS]
    lines.append(f"-# Desktop: downloads of {release['tag_name']} since its release on "
                 f"{date.fromisoformat(release_day(release)):%-d %B}, updates included; "
                 "Desktop has no active-user count.")
    if revisions:
        lines.append("-# The latest exports revised " + ", ".join(
            f"{LABELS[p]} {date.fromisoformat(d):%-d %b} {figure(old)} → {figure(new)}"
            for p, d, old, new in revisions))
    return "\n".join(lines)


def reminder_message(month_end, missing, inbox):
    names = " and ".join(LABELS[p] for p in missing)
    day = f"{month_end:%-d %B}"
    return "\n".join([
        f"⏰ **{names} active users for {month_end:%B %Y} {'are' if len(missing) > 1 else 'is'} "
        "missing.** Export:",
        *(f"- {LABELS[p]}: " + HOW_TO_EXPORT[p].format(day=day) for p in missing),
        "Then copy each to the inbox, and the figures post as soon as the last one lands:",
        f'`rsync <export>.csv {os.environ.get("MAU_INBOX_HOST") or socket.getfqdn()}:{inbox}/`',
    ])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.strip().split("\n")[0])
    parser.add_argument("--state", required=True, metavar="DIR",
                        help="Holds inbox/, done/, rejected/ and history.json.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would be posted; move and write nothing.")
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
        step("reading Desktop downloads")
        session = http.Session()
        session.headers.update({"Accept": "application/vnd.github.v3+json"})
        message = report_message(month_end, history, revisions, desktop_downloads(session))
    elif today.day >= REMIND_DAY:
        message = reminder_message(month_end, missing, inbox)
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
