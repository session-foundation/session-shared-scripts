#!/usr/bin/env python3
"""
Alert when a scheduled job has stopped succeeding.

OnFailure= reports a run that failed. It says nothing about a run that never
happened: a timer left disabled after an update, a unit renamed, a host that was
down through a whole schedule. This catches those, by age rather than by event.

Each job's unit touches /var/lib/session-ops/stamps/<name> when it succeeds
(ExecStartPost=, which a oneshot runs only after every ExecStart= exited 0). This
runs hourly on a timer of its own and posts one message listing every job in
jobs.toml whose stamp is older than its `max_age_hours`, then repeats it once a day
while the job stays silent. A job with no stamp at all is measured from the first
check that found it missing, so installing this does not alert on jobs that simply
have not run since.

Success is quiet: nothing is posted when every job is on time.

Config:
    ALERT_DISCORD_WEBHOOK_URL   where the alert goes (not needed with --dry-run)

Usage:
    session-ops-silence --state /var/lib/session-ops/silence/state.json
    session-ops-silence --dry-run        # print each job's age and the alert, post nothing
"""
import argparse
import json
import os
import socket
import sys
import time
import tomllib
from datetime import datetime, timezone

import requests

from session_ops.shared import discord
from session_ops.shared.env import get_env

REGISTRY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "jobs.toml")
STAMPS_DIR = "/var/lib/session-ops/stamps"
REMIND_SECONDS = 24 * 3600


def load_jobs(path):
    with open(path, "rb") as handle:
        jobs = tomllib.load(handle).get("job", [])
    for job in jobs:
        if not job.get("name") or not isinstance(job.get("max_age_hours"), (int, float)):
            sys.exit(f"{path}: every [[job]] needs a name and a numeric max_age_hours: {job}")
    return jobs


def stamp_time(stamps_dir, name):
    try:
        return os.stat(os.path.join(stamps_dir, name)).st_mtime
    except FileNotFoundError:
        return None


def load_state(path):
    """Per-job bookkeeping, or {} for anything unreadable: the worst a lost file
    costs is one alert repeated early, or a missing stamp's clock restarting."""
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(path, state):
    temporary = f"{path}.tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2, sort_keys=True)
    os.replace(temporary, path)


def evaluate(jobs, stamps_dir, state, now):
    """Return (silent, due): every job past its max age, and the ones to post now.

    Each entry is (job, last_success, silent_since), `last_success` None when no
    stamp exists. `state` is updated in place except for `alerted_at`, which only
    a delivered alert may set.
    """
    silent, due = [], []
    for job in jobs:
        entry = state.setdefault(job["name"], {})
        last = stamp_time(stamps_dir, job["name"])
        if last is None:
            since = entry.setdefault("missing_since", now)
        else:
            entry.pop("missing_since", None)
            since = last
        if now - since <= job["max_age_hours"] * 3600:
            entry.pop("alerted_at", None)
            continue
        silent.append((job, last, since))
        if now - entry.get("alerted_at", 0) >= REMIND_SECONDS:
            due.append((job, last, since))
    known = {job["name"] for job in jobs}
    for name in [name for name in state if name not in known]:
        del state[name]
    return silent, due


def duration(seconds):
    hours = int(seconds // 3600)
    if hours < 48:
        return f"{hours}h"
    return f"{hours // 24}d {hours % 24}h"


def utc(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def build_message(due, host, now):
    lines = [f"🔕 **Scheduled jobs gone quiet** on `{host}`"]
    for job, last, since in due:
        name = job["name"]
        seen = (f"last success {utc(last)}" if last is not None
                else f"no success recorded since {utc(since)}")
        lines.append(f"• **{name}**: silent for **{duration(now - since)}** "
                     f"(allowed {job['max_age_hours']}h), {seen}.")
        lines.append(f"  `systemctl list-timers {name}.timer` · "
                     f"`journalctl -u {name}.service -n 50 --no-pager`")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Alert on jobs that stopped succeeding.")
    parser.add_argument("--registry", default=REGISTRY)
    parser.add_argument("--stamps", default=STAMPS_DIR)
    parser.add_argument("--state", metavar="PATH",
                        help="Alert bookkeeping; without it every run re-alerts.")
    parser.add_argument("--webhook", help="Discord webhook URL (else ALERT_DISCORD_WEBHOOK_URL).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print the alert instead of posting it; write no state.")
    args = parser.parse_args()

    webhook = get_env("ALERT_DISCORD_WEBHOOK_URL", args.webhook, required=not args.dry_run)
    jobs = load_jobs(args.registry)
    now = time.time()
    state = load_state(args.state)
    silent, due = evaluate(jobs, args.stamps, state, now)

    for job in jobs:
        last = stamp_time(args.stamps, job["name"])
        age = "never" if last is None else f"{duration(now - last)} ago"
        print(f"{job['name']}: last success {age} (allowed {job['max_age_hours']}h)")
    if not due:
        print(f"{len(silent)} silent, none due an alert." if silent else "All jobs on time.")
        if args.state and not args.dry_run:
            save_state(args.state, state)
        return

    message = build_message(due, socket.gethostname(), now)
    if args.dry_run:
        print(message)
        return
    if not discord.post_to_discord(requests.Session(), webhook, [{"content": message}]):
        # State still saved: a missing stamp's clock must survive a failed post.
        if args.state:
            save_state(args.state, state)
        sys.exit("Could not post the silence alert to Discord.")
    for job, _, _ in due:
        state[job["name"]]["alerted_at"] = now
    if args.state:
        save_state(args.state, state)
    print(f"Alerted on {len(due)} job(s).")


if __name__ == "__main__":
    main()
