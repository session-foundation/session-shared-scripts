#!/usr/bin/env python3
"""
Report a failed systemd unit to Discord: the OnFailure= backstop.

Invoked by `OnFailure=session-ops-alert@%n.service`, which passes the failing unit's
name, so a rename cannot unsubscribe a unit the way matching on a workflow name did.

A `session-ops run` reports its own failures, with more to say than this can, and
records the invocation it reported. This stays quiet for that invocation and speaks
for the rest: a run killed, timed out, or unable to reach Discord, and any unit that
is not a job, such as the Zendesk relay.

Posts over ALERT_DISCORD_WEBHOOK_URL, else ZENDESK_DISCORD_WEBHOOK_URL, rather than
anything with a bot token: a failure notifier should depend on as little as possible
of whatever just broke.

The failed unit's last journal line comes with it, so the channel says what broke
rather than only that something did. Reading the journal needs the unit to carry
SupplementaryGroups=systemd-journal; without it the excerpt is dropped and the alert
is what it always was, rather than failing.

Usage:
    session-ops-alert <unit>
"""
import os
import re
import socket
import subprocess
import sys

from session_ops.ops.runner import scrub
from session_ops.shared import http
from session_ops.shared.discord import post_to_discord
from session_ops.shared.env import get_env
from session_ops.zendesk.claude_cli import advice


EXCERPT_CHARS = 400


def unit_property(unit, name):
    """A property of `unit` as systemd has it, or "" when it cannot be asked."""
    try:
        done = subprocess.run(["systemctl", "show", "-p", name, "--value", unit],
                              capture_output=True, text=True, timeout=15, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return done.stdout.strip()


def journal_tail(unit, lines=25, invocation=None):
    """The last lines `unit` logged, or "" when the journal cannot be read.

    `invocation` keeps it to the run that failed: a run killed before it logged
    anything would otherwise be quoted with the previous run's last line.

    Never raises: an excerpt improves the message, it is not a precondition for
    sending one, and the alert is the last thing that should fail here.
    """
    # _SYSTEMD_UNIT= rather than -u: -u also returns PID 1's lines about the unit
    # ("<unit>: Failed with result 'exit-code'."), which say it failed but never why.
    try:
        match = [f"_SYSTEMD_INVOCATION_ID={invocation}"] if invocation else []
        done = subprocess.run(["journalctl", f"_SYSTEMD_UNIT={unit}", *match, "-n", str(lines),
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


RESULTS = {"timeout": "timed out", "signal": "was killed", "core-dump": "crashed",
           "oom-kill": "ran out of memory", "exit-code": "exited with an error",
           "watchdog": "stopped answering its watchdog"}


def build_message(unit, host, detail="", result=""):
    """What the channel gets: what broke, where, what it said, and where to look.

    The journalctl line stays even when the excerpt is there — one line is rarely the
    whole story, and a unit that failed before it said anything still leaves it empty.
    """
    how = f" ({RESULTS.get(result, result)})" if result else ""
    parts = [f"❌ **{unit}** failed on `{host}`{how}."]
    if detail:
        parts.append(f"> {detail}")
    parts += advice(detail)
    parts.append(f"`journalctl -u {unit} -n 50 --no-pager`")
    return "\n".join(parts)


def already_alerted(unit, state_root="/var/lib/session-ops"):
    """Whether this failure of a session-ops job was reported by the run itself."""
    job = re.fullmatch(r"session-ops@(.+)\.service", unit)
    if not job:
        return False
    try:
        with open(os.path.join(state_root, job.group(1), "alerted"), encoding="utf-8") as fh:
            marker = fh.read().strip()
    except OSError:
        return False
    return bool(marker) and marker == unit_property(unit, "InvocationID")


def main(argv=None):
    args = [arg.strip() for arg in (sys.argv[1:] if argv is None else argv)]
    if len(args) != 1 or not args[0]:
        sys.exit("usage: session-ops-alert <unit>")
    unit = args[0]
    if already_alerted(unit):
        print(f"{unit} reported this failure itself.")
        return
    webhook = (os.environ.get("ALERT_DISCORD_WEBHOOK_URL")
               or get_env("ZENDESK_DISCORD_WEBHOOK_URL"))
    invocation = unit_property(unit, "InvocationID") or None
    detail = scrub(last_job_line(journal_tail(unit, invocation=invocation)))
    message = build_message(unit, socket.gethostname(), detail=detail,
                            result=unit_property(unit, "Result"))
    payload = {"content": message, "allowed_mentions": {"parse": []}}
    # A fresh session, never a Zendesk one — that carries the API-token auth header,
    # and Discord has no business receiving it.
    if not post_to_discord(http.Session(), webhook, [payload]):
        sys.exit("Could not post the failure to Discord.")


if __name__ == "__main__":
    main()
