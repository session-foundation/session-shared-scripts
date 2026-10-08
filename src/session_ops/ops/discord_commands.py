"""The slash commands session-ops-discord.service answers, and their registration.

    session-ops discord-register     # with discord.env's variables in the environment

Registered on one guild rather than globally: a guild's commands update at once, and
nobody outside that server can see or run them. They start hidden from every member
but server administrators; the server's Integrations settings grant them to a role or
channel. That only hides them, and the relay's allowlist is what refuses everyone else,
administrators included.
"""
import os
import sys

from session_ops.ops import registry
from session_ops.shared import discord, http

API = "https://discord.com/api/v10"
RUN = "run"
MAU_UPLOAD = "mau-upload"
MAU_JOB = "mau"

OPTION_STRING = 3
OPTION_ATTACHMENT = 11
CHAT_INPUT = 1
# Discord's cap on a choice's label.
MAX_CHOICE_NAME = 100


def commands(jobs):
    choices = [{"name": discord.clip(f"{job.name}: {job.description}", MAX_CHOICE_NAME),
                "value": job.name} for job in jobs if job.discord]
    hidden = {"type": CHAT_INPUT, "default_member_permissions": "0"}
    return [
        {**hidden, "name": RUN, "description": "Start a session-ops job now",
         "options": [{"type": OPTION_STRING, "name": "job", "required": True,
                      "description": "The job to start", "choices": choices}]},
        {**hidden, "name": MAU_UPLOAD,
         "description": "Hand the mau job a Play Console MAU export",
         "options": [{"type": OPTION_ATTACHMENT, "name": "file", "required": True,
                      "description": "The CSV the saved MAU report exports"}]},
    ]


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
