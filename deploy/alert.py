#!/usr/bin/env python3
"""
Report a failed systemd unit to the triage Discord channel.

Invoked by `OnFailure=zendesk-alert@%n.service`, which passes the failing unit's name.
This replaces notify_failure.yml for the jobs that left GitHub Actions — and improves
on it: that workflow matched on workflow *name*, so a rename silently unsubscribed
the job, whereas %n comes from the failing unit itself and cannot drift.

Posts over ZENDESK_DISCORD_WEBHOOK_URL rather than the bot token, deliberately. This
is one line of text needing no components, and a failure notifier should depend on as
little as possible of whatever just broke.

Usage:
    alert.py <name> [journal-unit]
"""
import os
import socket
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "zendesk_triage"))
import requests  # noqa: E402
import triage  # noqa: E402


def build_message(unit, host, journal_unit=None):
    """What the channel gets: what broke, where, and the one command that explains it.

    No counts and no detail — the unit may have failed before it had anything to
    report, and guessing at why would be worse than pointing at the journal.

    `journal_unit` is for a step that is not a unit of its own. The digest runs the
    resolver as its own first ExecStart, so naming that step in the journalctl line
    would send whoever reads it to a unit systemd has never heard of.
    """
    origin = f", as part of {journal_unit}" if journal_unit else ""
    return (f"❌ **{unit}** failed on `{host}`{origin}.\n"
            f"`journalctl -u {journal_unit or unit} -n 50 --no-pager`")


def main():
    args = [arg.strip() for arg in sys.argv[1:]]
    if not args or len(args) > 2 or not args[0]:
        sys.exit("usage: alert.py <name> [journal-unit]")
    webhook = triage.get_env("ZENDESK_DISCORD_WEBHOOK_URL")
    message = build_message(args[0], socket.gethostname(), *args[1:])
    # A fresh session, never a Zendesk one — that carries the API-token auth header,
    # and Discord has no business receiving it.
    if not triage.post_to_discord(requests.Session(), webhook, [{"content": message}]):
        sys.exit("Could not post the failure to Discord.")


if __name__ == "__main__":
    main()
