#!/usr/bin/env python3
"""
Alert before a token the jobs use expires.

Each token's expiry comes from one of three places:

- GITHUB_PRS_TOKEN: GitHub's response header, so a rotation needs nothing updated.
- CLAUDE_CODE_OAUTH_TOKEN: a year after this job first saw it, by a fingerprint kept
  in --state, so a rotation needs nothing updated either. A token installed before the
  job existed gets its real issue date from [issued] in the expiry file, which applies
  only while that same token is in place.
- CROWDIN_API_TOKEN: exposes its expiry to no API, so it is recorded by hand, and
  reported until it is.

/etc/session-ops/expiry.toml:

    [expires]
    # A date, or "never" when it does not expire or this host does not use it.
    CROWDIN_API_TOKEN = "never"

    [issued.CLAUDE_CODE_OAUTH_TOKEN]
    # As printed by --dry-run.
    fingerprint = "3f2a9c01b4de"
    date = 2025-11-20

Posts once as each token comes within 14 days, 7 days and 24 hours of expiring, and
once more when it has expired; a new expiry date starts it over. Times are UTC, and a
date without one counts from its start, so a run on any timezone's morning errs early.
Success is quiet.

Config:
    ALERT_DISCORD_WEBHOOK_URL   where the alert goes (not needed with --dry-run)
    GITHUB_PRS_TOKEN            probed for its expiry; skipped when unset
    CLAUDE_CODE_OAUTH_TOKEN     fingerprinted; skipped when unset

Usage:
    session-ops-token-expiry --state /var/lib/session-ops/token-expiry/state.json
    session-ops-token-expiry --dry-run      # print every token's expiry and the alert
"""
import argparse
import hashlib
import json
import os
import socket
import sys
import tomllib
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from session_ops.shared import discord, http
from session_ops.shared.env import get_env

EXPIRY_FILE = "/etc/session-ops/expiry.toml"
THRESHOLDS = (1, 7, 14)
NEVER = "never"
GITHUB, RECORDED, FIRST_SEEN = "github", "recorded", "first seen"
GITHUB_RATE_LIMIT = "https://api.github.com/rate_limit"
GITHUB_EXPIRY_HEADER = "github-authentication-token-expiration"


@dataclass(frozen=True)
class Token:
    env_file: str
    renew: str
    source: str
    lifetime_days: int = None


TOKENS = {
    "GITHUB_PRS_TOKEN": Token("/etc/session-ops/github-prs.env",
                              "GitHub → Settings → Developer settings → Personal access tokens",
                              GITHUB),
    "CROWDIN_API_TOKEN": Token("/etc/session-ops/crowdin.env",
                               "https://crowdin.com/settings#api-key", RECORDED),
    "CLAUDE_CODE_OAUTH_TOKEN": Token("/etc/session-ops/zendesk.env",
                                     "`claude setup-token` on a machine with a browser, "
                                     "then restart zendesk-relay",
                                     FIRST_SEEN, lifetime_days=365),
}


@dataclass(frozen=True)
class Config:
    expires: dict
    issued: dict


def start_of(day):
    return datetime(day.year, day.month, day.day, tzinfo=timezone.utc)


def as_date(value):
    return value.date() if isinstance(value, datetime) else value


def load_config(path):
    """The expiry file's [expires] and [issued]; empty when it does not exist yet."""
    try:
        with open(path, "rb") as handle:
            data = tomllib.load(handle)
    except FileNotFoundError:
        return Config({}, {})
    except tomllib.TOMLDecodeError as exc:
        sys.exit(f"{path}: {exc}")
    expires = {}
    for name, value in data.get("expires", {}).items():
        value = as_date(value)
        if not (isinstance(value, date) or value == NEVER):
            sys.exit(f"{path}: {name} must be a date (2027-03-01) or \"{NEVER}\", not {value!r}")
        expires[name] = value
    issued = {}
    for name, entry in data.get("issued", {}).items():
        when = as_date(entry.get("date")) if isinstance(entry, dict) else None
        if not (isinstance(when, date) and isinstance(entry.get("fingerprint"), str)):
            sys.exit(f"{path}: [issued.{name}] needs a fingerprint and a date")
        issued[name] = (entry["fingerprint"], when)
    return Config(expires, issued)


def fingerprint(value):
    """Enough of a hash to tell tokens apart, and nothing that helps recover one."""
    return hashlib.sha256(value.encode()).hexdigest()[:12]


def issued_on(name, value, issued, seen, today):
    """When the token in `value` was issued: its [issued] date while that names this
    token, else the day it was first seen. Records it in `seen`."""
    current = fingerprint(value)
    recorded = issued.get(name)
    entry = seen.get(name, {})
    if recorded and recorded[0] == current:
        since = recorded[1]
    elif entry.get("fingerprint") == current:
        since = date.fromisoformat(entry["since"])
    else:
        since = today
    seen[name] = {"fingerprint": current, "since": since.isoformat()}
    return since


def parse_github_expiry(value):
    """The time in GitHub's expiry header, "2027-10-02 03:00:00 UTC"."""
    try:
        return datetime.strptime(value.removesuffix(" UTC")[:19], "%Y-%m-%d %H:%M:%S") \
            .replace(tzinfo=timezone.utc)
    except ValueError:
        raise ValueError(f"unreadable {GITHUB_EXPIRY_HEADER}: {value!r}") from None


def probe_github(session, token):
    """The token's expiry time, NEVER, or "rejected" when GitHub refuses it."""
    resp = session.request("GET", GITHUB_RATE_LIMIT,
                           headers={"Authorization": f"Bearer {token}"})
    if resp.status_code == 401:
        return "rejected"
    resp.raise_for_status()
    header = resp.headers.get(GITHUB_EXPIRY_HEADER)
    return parse_github_expiry(header) if header else NEVER


def level(expiry, now):
    """What an alert about `expiry` would say, or None while it needs none."""
    if expiry in ("missing", "rejected"):
        return expiry
    if expiry == NEVER:
        return None
    left = expiry - now
    if left <= timedelta(0):
        return "expired"
    return next((f"{t}d" for t in THRESHOLDS if left <= timedelta(days=t)), None)


def evaluate(expiries, state, now):
    """The tokens due an alert, as (name, expiry, level). `state` drops every token
    that needs no alert, so a renewed one starts over; recording an alert is left to
    a delivered post."""
    due = []
    for name, expiry in expiries.items():
        current = level(expiry, now)
        if current is None:
            state.pop(name, None)
        elif state.get(name) != record(expiry, current):
            due.append((name, expiry, current))
    for name in [name for name in state if name not in expiries]:
        del state[name]
    return due


def record(expiry, current):
    return {"level": current, "expires": shown(expiry) if isinstance(expiry, datetime) else None}


def shown(when):
    return when.strftime("%Y-%m-%d %H:%M UTC")


WITHIN = {"1d": "in less than 24h", "7d": "in less than 7 days", "14d": "in less than 14 days"}


def describe(expiry, current, expiry_file):
    if current == "missing":
        return f"no expiry date in `{expiry_file}`"
    if current == "rejected":
        return "GitHub rejects it (401)"
    if current == "expired":
        return f"**expired** at {shown(expiry)}"
    return f"expires **{shown(expiry)}**, {WITHIN[current]}"


def build_message(due, host, expiry_file):
    lines = [f"🔑 **Token expiry** on `{host}`"]
    for name, expiry, current in due:
        token = TOKENS[name]
        lines.append(f"• **{name}**: {describe(expiry, current, expiry_file)}.")
        if current == "missing":
            continue
        then = f", then its date in `{expiry_file}`" if token.source == RECORDED else ""
        lines.append(f"  Renew: {token.renew}; update `{token.env_file}`{then}.")
    return "\n".join(lines)


def utc_now():
    return datetime.now(timezone.utc)


def load_state(path):
    """{"alerts": ..., "seen": ...}. Losing it re-dates a fingerprinted token from the
    next run, which alerts late: the reason it is saved even when a post fails."""
    data = {}
    if path and os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            data = {}
    data = data if isinstance(data, dict) else {}
    return {"alerts": data.get("alerts", {}), "seen": data.get("seen", {})}


def save_state(path, state):
    temporary = f"{path}.tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2, sort_keys=True)
    os.replace(temporary, path)


def collect(config, environ, seen, session, now):
    """{name: expiry} for every token in use. A token set nowhere is left out, and so
    is its fingerprint."""
    expiries = {}
    for name, token in TOKENS.items():
        value = environ.get(name)
        if token.source == RECORDED:
            expiry = config.expires.get(name, "missing")
            expiries[name] = start_of(expiry) if isinstance(expiry, date) else expiry
        elif not value:
            seen.pop(name, None)
        elif token.source == GITHUB:
            expiries[name] = probe_github(session, value)
        else:
            since = issued_on(name, value, config.issued, seen, now.date())
            expiries[name] = start_of(since + timedelta(days=token.lifetime_days))
    return expiries


def main(argv=None):
    parser = argparse.ArgumentParser(description="Alert before a token expires.")
    parser.add_argument("--expiry-file", default=EXPIRY_FILE)
    parser.add_argument("--state", metavar="PATH",
                        help="Alert bookkeeping; without it every run re-alerts.")
    parser.add_argument("--webhook", help="Discord webhook URL (else ALERT_DISCORD_WEBHOOK_URL).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print the alert instead of posting it; write no state.")
    args = parser.parse_args(argv)

    webhook = get_env("ALERT_DISCORD_WEBHOOK_URL", args.webhook, required=not args.dry_run)
    now = utc_now()
    state = load_state(args.state)
    expiries = collect(load_config(args.expiry_file), os.environ, state["seen"],
                       http.Session(), now)
    for name in TOKENS:
        expiry = expiries.get(name, "not set")
        line = f"{name}: {shown(expiry) if isinstance(expiry, datetime) else expiry}"
        seen = state["seen"].get(name)
        if seen:
            line += f" (fingerprint {seen['fingerprint']}, issued {seen['since']})"
        print(line)

    due = evaluate(expiries, state["alerts"], now)
    persist = bool(args.state) and not args.dry_run
    if not due:
        print("Nothing due an alert.")
        if persist:
            save_state(args.state, state)
        return

    message = build_message(due, socket.gethostname(), args.expiry_file)
    if args.dry_run:
        print(message)
        return
    if not discord.post_to_discord(http.Session(), webhook, [{"content": message}]):
        if persist:
            save_state(args.state, state)
        sys.exit("Could not post the token expiry alert to Discord.")
    for name, expiry, current in due:
        state["alerts"][name] = record(expiry, current)
    if persist:
        save_state(args.state, state)
    print(f"Alerted on {len(due)} token(s).")


if __name__ == "__main__":
    main()
