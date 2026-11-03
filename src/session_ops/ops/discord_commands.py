"""The slash commands session-ops-discord.service answers, and their registration.

    session-ops discord-register     # with discord.env's variables in the environment

Registered on one guild rather than globally: a guild's commands update at once, and
nobody outside that server can see or run them. Both are shown to every member, since
Discord lets only someone who can use a command change who sees it; the relay's
allowlist is what refuses /run to everyone else. The server's Integrations settings can
still narrow where they show.
"""
import os
import sys

from session_ops.ops import registry
from session_ops.shared import discord, http

API = "https://discord.com/api/v10"
RUN = "run"
VERSIONS = "versions"
STORE_VERSIONS_JOB = "store-versions"

OPTION_STRING = 3
CHAT_INPUT = 1
# Discord's cap on a choice's label.
MAX_CHOICE_NAME = 100


def commands(jobs):
    choices = [{"name": discord.clip(f"{job.name}: {job.description}", MAX_CHOICE_NAME),
                "value": job.name} for job in jobs if job.discord]
    found = [{"type": CHAT_INPUT, "name": RUN, "description": "Start a session-ops job now",
              "options": [{"type": OPTION_STRING, "name": "job", "required": True,
                           "description": "The job to start", "choices": choices}]}]
    if any(job.name == STORE_VERSIONS_JOB for job in jobs):
        found.append({"type": CHAT_INPUT, "name": VERSIONS,
                      "description": "Each Session platform's store and GitHub versions"})
    return found


def register(session, app_id, guild_id, token, jobs):
    """Replace every command on the guild with these. Returns the names registered."""
    response = session.request("PUT", f"{API}/applications/{app_id}/guilds/{guild_id}/commands",
                               headers={"Authorization": f"Bot {token}"}, json=commands(jobs))
    if response.status_code != 200:
        raise SystemExit(f"Discord refused the commands: {response.status_code} "
                         f"{response.text[:300]}")
    return [command["name"] for command in response.json()]


def main():
    missing = [name for name in ("DISCORD_APP_ID", "DISCORD_GUILD_ID", "DISCORD_BOT_TOKEN")
               if not os.environ.get(name)]
    if missing:
        sys.exit(f"missing {', '.join(missing)} in the environment")
    names = register(http.Session(attempts=3), os.environ["DISCORD_APP_ID"],
                     os.environ["DISCORD_GUILD_ID"], os.environ["DISCORD_BOT_TOKEN"],
                     registry.load())
    print("Registered /" + ", /".join(names))
