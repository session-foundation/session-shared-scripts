"""
Monthly active users, posted once a month: Android's from the Play Console exports
dropped into the job's inbox, and the latest Desktop release's downloads, which Desktop
has in place of active users.

    session-ops run mau [--dry-run]
    rsync "All countries _ regions.csv" root@<host>:/var/lib/session-ops/mau/inbox/

Every export's daily figures merge into history.json, so an export may cover any range
and overlap the previous one. A month is posted once its last day is in the history;
until then, from the REMIND_DAY on, each run posts a reminder instead.
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
from session_ops.platforms import release_stats
from session_ops.shared import discord, http

MAU_COLUMN = ("Monthly Active Users (MAU) (Unique users, Per interval, Daily): "
              "All countries / regions")
EXPORT_DATE = "%b %d, %Y"
# Play's daily figures trail by about eight days, so a month's last day lands around the 9th.
REMIND_DAY = 10
ZONE = ZoneInfo("Australia/Melbourne")
VERSION = 1
HISTORY = "history.json"


class Rejected(ValueError):
    pass


def parse_export(path):
    """{ISO date: MAU} from a Play Console export of the saved MAU report."""
    try:
        with open(path, encoding="utf-8-sig", newline="") as handle:
            # strict: a quoted figure cut off at the end of the file is an error, not a smaller figure.
            rows = list(csv.reader(handle, strict=True))
    except (UnicodeDecodeError, csv.Error) as exc:
        raise Rejected(f"not a CSV export ({exc})") from None
    if not rows or not rows[0] or rows[0][0] != "Date" or MAU_COLUMN not in rows[0]:
        raise Rejected(f"no Date and “{MAU_COLUMN}” columns: export the saved MAU report "
                       "(unique users, per interval, daily, all countries) from a Console "
                       "set to English")
    column = rows[0].index(MAU_COLUMN)
    days = {}
    for line, row in enumerate(rows[1:], start=2):
        try:
            day = datetime.strptime(row[0], EXPORT_DATE).date().isoformat()
            figure = row[column].replace(",", "")
        except (IndexError, ValueError):
            raise Rejected(f"line {line} is not a date and a figure: {row[:column + 1]}") from None
        if not figure:
            continue
        if not figure.isdigit():
            raise Rejected(f"line {line} has {row[column]!r} for a figure")
        days[day] = int(figure)
    if not days:
        raise Rejected("no figures in it")
    return days


def read_inbox(inbox):
    """(path, days) for each export, oldest first, and (path, reason) for each rejected one.

    glob skips dotfiles, so rsync's temporary file is never read half-written.
    """
    exports, rejected = [], []
    for path in sorted(glob.glob(os.path.join(inbox, "*.csv")), key=os.path.getmtime):
        try:
            exports.append((path, parse_export(path)))
        except Rejected as exc:
            rejected.append((path, str(exc)))
    return exports, rejected


def merge(history, exports):
    """Merge each export into `history`, the later export winning; returns the revisions,
    (day, old, new), for the figures a later export changed."""
    revisions = []
    for _, days in exports:
        for day, figure in sorted(days.items()):
            old = history.get(day)
            if old is not None and old != figure:
                revisions.append((day, old, figure))
            history[day] = figure
    return revisions


def file_away(path, folder):
    os.makedirs(folder, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    os.replace(path, os.path.join(folder, f"{stamp}-{os.path.basename(path)}"))


def load_history(path):
    """The history at `path`, empty if there is none yet. Unlike a digest's dedup cache it
    cannot be rebuilt from a re-run, so an unreadable one stops the run rather than reset."""
    if not os.path.exists(path):
        return {"version": VERSION, "android": {}, "posted": []}
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


def desktop_downloads(session):
    """(latest Desktop release, its downloads per platform)."""
    release = release_stats.latest(release_stats.fetch(session, "session-desktop"))
    flathub = release_stats.flathub_installs_since(
        release_stats.get_json(session, release_stats.FLATHUB), release_stats.release_day(release))
    return release, release_stats.platform_totals(release, flathub)


def report_message(month_end, android, revisions, desktop):
    month = month_end.strftime("%B %Y")
    current = android[month_end.isoformat()]
    release, downloads = desktop
    desktop_total = downloads["linux"] + downloads["macos"] + downloads["windows"]
    line = f"Android: **{figure(current)}**"
    before = android.get(previous_month_end(month_end).isoformat())
    if before:
        change = current - before
        line += (f" ({'+' if change >= 0 else '−'}{figure(abs(change))}, "
                 f"{change / before:+.1%} on {previous_month_end(month_end):%B})")
    lines = [
        f"📊 **Monthly active users, {month}**",
        line,
        f"Desktop: **{figure(desktop_total)}** (Linux {figure(downloads['linux'])} · "
        f"macOS {figure(downloads['macos'])} · Windows {figure(downloads['windows'])})",
        f"**Total: {figure(current + desktop_total)}**",
        f"-# Android: Play Console MAU on {month_end:%-d %B}, users who opened Session in the "
        "28 days before.",
        f"-# Desktop: downloads of {release['tag_name']} since its release on "
        f"{date.fromisoformat(release_stats.release_day(release)):%-d %B}, updates included; "
        "Desktop has no active-user count.",
    ]
    if revisions:
        lines.append("-# The latest export revised " + ", ".join(
            f"{date.fromisoformat(day):%-d %b} {figure(old)} → {figure(new)}"
            for day, old, new in revisions))
    return "\n".join(lines)


def reminder_message(month_end, inbox):
    return "\n".join([
        f"⏰ **Android MAU for {month_end:%B %Y} is missing.**",
        "In Play Console, open Statistics → Saved reports → the MAU report, check it covers "
        f"{month_end:%-d %B}, and Export report → CSV. Then copy it to the inbox, and the "
        "figures post as soon as it lands:",
        f'`rsync "<export>.csv" {os.environ.get("MAU_INBOX_HOST") or socket.getfqdn()}:{inbox}/`',
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
    revisions = merge(history["android"], exports)
    for day, old, new in revisions:
        print(f"{day}: {old} revised to {new}")
    if not args.dry_run:
        # Saved before the files move: a run stopped in between reads them again, harmlessly.
        save_history(history_path, history)
        for path, _ in exports:
            file_away(path, os.path.join(args.state, "done"))
        for path, _ in rejected:
            file_away(path, os.path.join(args.state, "rejected"))

    today = datetime.now(ZONE).date()
    month_end = previous_month_end(today)
    month = month_end.strftime("%Y-%m")
    message = None
    if month in history["posted"]:
        print(f"{month} already posted.")
    elif month_end.isoformat() in history["android"]:
        step("reading Desktop downloads")
        session = http.Session()
        session.headers.update({"Accept": "application/vnd.github.v3+json"})
        message = report_message(month_end, history["android"], revisions,
                                 desktop_downloads(session))
    elif today.day >= REMIND_DAY:
        message = reminder_message(month_end, inbox)
    else:
        print(f"Waiting for {month_end}; reminders start on the {REMIND_DAY}th.")

    if message:
        step("posting to Discord")
        if args.dry_run:
            print(message)
        else:
            payload = {"content": message, "allowed_mentions": {"parse": []}}
            if not discord.post_to_discord(http.Session(), os.environ["MAU_DISCORD_WEBHOOK_URL"],
                                           [payload]):
                raise RuntimeError("Discord did not accept the message")
            if month_end.isoformat() in history["android"]:
                history["posted"].append(month)
                save_history(history_path, history)
    if rejected:
        raise SystemExit("rejected " + "; ".join(
            f"{os.path.basename(path)}: {reason}" for path, reason in rejected))


if __name__ == "__main__":
    main()
