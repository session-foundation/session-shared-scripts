#!/usr/bin/env python3
"""
Report a failed systemd unit to the triage Discord channel.

Invoked by `OnFailure=zendesk-alert@%n.service`, which passes the failing unit's name.
This replaces notify_failure.yml for the jobs that left GitHub Actions — and improves
on it: that workflow matched on workflow *name*, so a rename silently unsubscribed
the job, whereas %n comes from the failing unit itself and cannot drift.

Posts over ZENDESK_DISCORD_WEBHOOK_URL rather than the bot token, deliberately. This
is one line of text needing no components, and a failure notifier should depend on as
little as possible of whatever just broke. ALERT_DISCORD_WEBHOOK_URL overrides it, so
a job that posts to a channel of its own reports its failures there too.

The failed unit's last journal line comes with it, so the channel says what broke
rather than only that something did. Reading the journal needs the unit to carry
SupplementaryGroups=systemd-journal; without it the excerpt is dropped and the alert
is what it always was, rather than failing.

Usage:
    alert.py <name> [journal-unit]
"""
import os
import socket
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "zendesk_triage"))
import requests  # noqa: E402
import triage  # noqa: E402


# A dead login reads as a broken job unless the alert names it: the job is fine and
# re-running it fixes nothing. The CLI's wording varies between refusals, so this
# matches the words that survive the rewordings. Only a line the CLI path wrote is
# checked: Zendesk's own 401 body says "Couldn't authenticate you".
AUTH_SIGNATURES = ("oauth", "/login", "authenticate", "invalid api key",
                   "unauthorized", "credit balance", "signed in")
CLI_FAILURE_PREFIX = f"{triage.CLAUDE_CLI} exited"
# Where the Claude Code CLI lives for the account the units run as; see
# deploy/README.md. Spelled out because an alert that says "log in again" without
# saying how sends whoever is on call to the README first.
RELOGIN = ("runuser -u zendesk -- env HOME=/home/zendesk "
           "/home/zendesk/.local/bin/claude   # then /login")
EXCERPT_CHARS = 400


def journal_tail(unit, lines=25):
    """The last lines `unit` logged, or "" when the journal cannot be read.

    Never raises: an excerpt improves the message, it is not a precondition for
    sending one, and the alert is the last thing that should fail here.
    """
    # _SYSTEMD_UNIT= rather than -u: -u also returns PID 1's lines about the unit
    # ("<unit>: Failed with result 'exit-code'."), which say it failed but never why.
    try:
        done = subprocess.run(["journalctl", f"_SYSTEMD_UNIT={unit}", "-n", str(lines),
                               "--no-pager", "-q", "-o", "cat"],
                              capture_output=True, text=True, timeout=15, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return done.stdout if done.returncode == 0 else ""


def last_job_line(journal):
    """The last non-blank line the job logged, clipped."""
    for line in reversed((journal or "").splitlines()):
        line = line.strip()
        if line:
            return line[:EXCERPT_CHARS]
    return ""


def is_auth_failure(line):
    line = (line or "").lower()
    return (line.startswith(CLI_FAILURE_PREFIX)
            and any(signature in line for signature in AUTH_SIGNATURES))


def build_message(unit, host, journal_unit=None, detail=""):
    """What the channel gets: what broke, where, what it said, and where to look.

    The journalctl line stays even when the excerpt is there — one line is rarely the
    whole story, and a unit that failed before it said anything still leaves it empty.

    `journal_unit` is for a step that is not a unit of its own. The digest runs the
    resolver as its own first ExecStart, so naming that step in the journalctl line
    would send whoever reads it to a unit systemd has never heard of.
    """
    origin = f", as part of {journal_unit}" if journal_unit else ""
    parts = [f"❌ **{unit}** failed on `{host}`{origin}."]
    if detail:
        parts.append(f"> {detail}")
    if is_auth_failure(detail):
        parts.append("**The Claude Code CLI is no longer logged in.** Re-running the "
                     "unit will not fix it — log in again as the service account:")
        parts.append(f"```\n{RELOGIN}\n```")
    parts.append(f"`journalctl -u {journal_unit or unit} -n 50 --no-pager`")
    return "\n".join(parts)


def main():
    args = [arg.strip() for arg in sys.argv[1:]]
    if not args or len(args) > 2 or not args[0]:
        sys.exit("usage: alert.py <name> [journal-unit]")
    webhook = (os.environ.get("ALERT_DISCORD_WEBHOOK_URL")
               or triage.get_env("ZENDESK_DISCORD_WEBHOOK_URL"))
    detail = last_job_line(journal_tail(args[-1]))
    message = build_message(args[0], socket.gethostname(), *args[1:], detail=detail)
    # A fresh session, never a Zendesk one — that carries the API-token auth header,
    # and Discord has no business receiving it.
    if not triage.post_to_discord(requests.Session(), webhook, [{"content": message}]):
        sys.exit("Could not post the failure to Discord.")


if __name__ == "__main__":
    main()
