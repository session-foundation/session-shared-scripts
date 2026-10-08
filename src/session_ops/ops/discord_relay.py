"""Discord slash commands for session-ops jobs: POST /discord/interactions.

    /run job:<name>          starts session-ops@<name>.service
    /mau-upload file:<csv>   puts a Play Console export in mau's inbox, whose path unit
                             then runs the job

Discord signs every interaction with the app's Ed25519 key. One is refused unless it
comes from DISCORD_GUILD_ID and from someone in ALLOWED_USER_IDS or ALLOWED_ROLE_IDS.
The account this runs as may start only the jobs jobs.toml marks `discord`: the polkit
rule `session-ops polkit` writes refuses it everything else.

Config (env vars, /etc/session-ops/discord.env):
    DISCORD_PUBLIC_KEY   the app's public key. Unset refuses every request
    DISCORD_GUILD_ID     the one server it answers
    ALLOWED_USER_IDS     comma-separated Discord user ids
    ALLOWED_ROLE_IDS     comma-separated role ids

Usage:
    uvicorn session_ops.ops.discord_relay:app --host 127.0.0.1 --port 8081
"""
import json
import os
import subprocess
import time
from urllib.parse import urlsplit

from fastapi import BackgroundTasks, FastAPI, Request, Response
from nacl.exceptions import BadSignatureError
from nacl.signing import VerifyKey
from starlette.concurrency import run_in_threadpool

from session_ops.ops import registry
from session_ops.ops.discord_commands import API, MAU_JOB, MAU_UPLOAD, RUN
from session_ops.platforms import mau
from session_ops.shared import http

# Bounds replay of a request Discord genuinely signed; the signature covers the timestamp.
MAX_SIGNATURE_AGE_SECONDS = 300
# Without it, a host whose clock trails Discord's refuses every interaction, the
# endpoint-registering PING included.
MAX_CLOCK_SKEW_SECONDS = 60
# A year of daily figures is a few kilobytes.
MAX_UPLOAD_BYTES = 1 << 20
ATTACHMENT_HOSTS = frozenset({"cdn.discordapp.com", "media.discordapp.net"})
SYSTEMCTL_TIMEOUT_SECONDS = 10
RUNNING_STATES = frozenset({"active", "activating", "deactivating", "reloading"})

INTERACTION_PING = 1
INTERACTION_COMMAND = 2
RESPONSE_PONG = 1
RESPONSE_MESSAGE = 4
RESPONSE_DEFERRED = 5
EPHEMERAL = 64
NO_PINGS = {"parse": []}

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


def env(name, default=None):
    return os.environ.get(name, default)


def id_list(raw):
    return [item.strip() for item in (raw or "").split(",") if item.strip()]


def signature_ok(raw, signature, timestamp, key):
    if not (signature and timestamp and key):
        return False
    try:
        age = time.time() - float(timestamp)
    except (TypeError, ValueError):
        return False
    if age < -MAX_CLOCK_SKEW_SECONDS or age > MAX_SIGNATURE_AGE_SECONDS:
        return False
    try:
        VerifyKey(bytes.fromhex(key)).verify(timestamp.encode() + raw, bytes.fromhex(signature))
    except (BadSignatureError, ValueError):
        return False
    return True


def user_id(interaction):
    member = interaction.get("member") or {}
    return (member.get("user") or interaction.get("user") or {}).get("id")


def refusal(interaction):
    """Why this person may not use the commands here, or None.

    Both allowlists empty refuses everybody: an unconfigured relay must not mean an open one.
    A DM carries no guild_id, so it is refused with the wrong server.
    """
    guild = env("DISCORD_GUILD_ID")
    if not guild or interaction.get("guild_id") != guild:
        return "These commands do not work here."
    users = id_list(env("ALLOWED_USER_IDS"))
    roles = id_list(env("ALLOWED_ROLE_IDS"))
    if not users and not roles:
        return "Neither ALLOWED_USER_IDS nor ALLOWED_ROLE_IDS is set on the relay."
    if user_id(interaction) in users:
        return None
    if any(role in roles for role in (interaction.get("member") or {}).get("roles") or []):
        return None
    return "You are not on the list of people who can run session-ops jobs."


def reply(text, ephemeral=True):
    data = {"content": text, "allowed_mentions": NO_PINGS}
    if ephemeral:
        data["flags"] = EPHEMERAL
    return {"type": RESPONSE_MESSAGE, "data": data}


def option(interaction, name):
    for item in (interaction.get("data") or {}).get("options") or []:
        if item.get("name") == name:
            return item.get("value")
    return None


def systemctl(*args):
    return subprocess.run(["systemctl", "--no-ask-password", *args], check=False,
                          capture_output=True, text=True, timeout=SYSTEMCTL_TIMEOUT_SECONDS)


def unit_name(job_name):
    return f"session-ops@{job_name}.service"


def busy(job):
    """The first of `job` and the queue's jobs that is running or waiting to, else None.

    The queue runs its jobs one at a time so no two share the host or their APIs; a run
    started from here keeps to that.
    """
    units = [unit_name(name) for name in dict.fromkeys([job.name, *registry.load_queue().jobs])]
    waiting = {line.split()[1] for line in
               systemctl("list-jobs", "--no-legend", "--plain").stdout.splitlines()
               if len(line.split()) > 1}
    states = systemctl("is-active", *units).stdout.split()
    if len(states) != len(units):
        raise RuntimeError(f"systemctl is-active gave {len(states)} states for {len(units)} units")
    for unit, state in zip(units, states):
        if unit in waiting or state in RUNNING_STATES:
            return unit
    return None


def handle_run(interaction):
    name = option(interaction, "job")
    job = next((job for job in registry.load() if job.discord and job.name == name), None)
    if job is None:
        return reply(f"❌ `{name}` is not a job /run can start.")
    unit = unit_name(job.name)
    try:
        blocking = busy(job)
        started = None if blocking else systemctl("start", "--no-block", unit)
    except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
        print(f"/run {job.name}: {exc!r}", flush=True)
        return reply("❌ Could not ask systemd on the host; see "
                     "`journalctl -u session-ops-discord`.")
    if blocking:
        return reply(f"⏳ `{blocking}` is running or waiting to; try again once it has finished.")
    if started.returncode != 0:
        error = started.stderr.strip()
        print(f"/run {job.name} by {user_id(interaction)}: {error}", flush=True)
        return reply(f"❌ systemd did not start **{job.name}**: {error[:300]}")
    print(f"/run {job.name} by {user_id(interaction)}: started", flush=True)
    return reply(f"▶️ <@{user_id(interaction)}> started **{job.name}**. It posts in its own "
                 f"channel when it is done.", ephemeral=False)


def attachment_problem(attachment):
    if not attachment:
        return "Discord sent no file with the command."
    filename = attachment.get("filename") or ""
    if not filename.lower().endswith(".csv"):
        return f"Play Console exports a .csv, and this is `{filename}`."
    if not isinstance(attachment.get("size"), int) or attachment["size"] > MAX_UPLOAD_BYTES:
        return f"`{filename}` is larger than any MAU export, at {attachment.get('size')} bytes."
    url = urlsplit(attachment.get("url") or "")
    if url.scheme != "https" or url.hostname not in ATTACHMENT_HOSTS:
        return "The file is not on Discord's CDN."
    return None


def inbox():
    return os.path.dirname(registry.get(MAU_JOB).watch_glob)


def place_export(session, attachment, name, uploader):
    """Download the export into mau's inbox if it parses. Returns the message for Discord."""
    response = session.request("GET", attachment["url"])
    if response.status_code != 200:
        return f"❌ Discord's CDN answered {response.status_code} for the file; send it again."
    if len(response.content) > MAX_UPLOAD_BYTES:
        return "❌ The file Discord served is larger than any MAU export."
    folder = inbox()
    # mau's glob skips dotfiles, so its path unit cannot start on a file still being checked.
    hidden = os.path.join(folder, f".{name}")
    try:
        with open(hidden, "wb") as handle:
            handle.write(response.content)
            # mau runs as another account; the inbox's 0770 keeps everyone else out.
            os.fchmod(handle.fileno(), 0o644)
        try:
            days = mau.parse_export(hidden)
        except mau.Rejected as exc:
            return f"❌ That is not the export mau reads: {exc}"
        os.replace(hidden, os.path.join(folder, name))
    finally:
        if os.path.exists(hidden):
            os.unlink(hidden)
    return (f"📥 <@{uploader}>'s export, {len(days)} days from {min(days)} to {max(days)}, "
            f"is in mau's inbox, and the job is running on it.")


def edit_original(session, interaction, text):
    url = (f"{API}/webhooks/{interaction['application_id']}/{interaction['token']}"
           f"/messages/@original")
    try:
        session.request("PATCH", url, attempts=2,
                        json={"content": text, "allowed_mentions": NO_PINGS})
    except Exception as exc:  # noqa: BLE001 — the outcome is in the journal either way
        print(f"could not tell Discord: {exc!r}", flush=True)


def drop_export(interaction, attachment):
    """Never raises: Discord shows "thinking…" until this edits that message."""
    session = http.Session(attempts=3, timeout=30)
    uploader = user_id(interaction)
    try:
        outcome = place_export(session, attachment, f"discord-{interaction['id']}.csv", uploader)
    except Exception as exc:  # noqa: BLE001 — reported to Discord below
        print(f"/mau-upload by {uploader}: {exc!r}", flush=True)
        outcome = "❌ The upload failed on the host; see `journalctl -u session-ops-discord`."
    print(f"/mau-upload by {uploader}: {outcome}", flush=True)
    edit_original(session, interaction, outcome)


def handle_mau_upload(interaction, background):
    resolved = ((interaction.get("data") or {}).get("resolved") or {}).get("attachments") or {}
    attachment = resolved.get(str(option(interaction, "file")))
    problem = attachment_problem(attachment)
    if problem:
        return reply(f"❌ {problem}")
    background.add_task(run_in_threadpool, drop_export, interaction, attachment)
    return {"type": RESPONSE_DEFERRED}


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.post("/discord/interactions")
async def interactions(request: Request, background: BackgroundTasks):
    raw = await request.body()
    if not signature_ok(raw, request.headers.get("x-signature-ed25519"),
                        request.headers.get("x-signature-timestamp"), env("DISCORD_PUBLIC_KEY")):
        return Response("bad signature", status_code=401)
    try:
        interaction = json.loads(raw)
    except ValueError:
        interaction = None
    if not isinstance(interaction, dict):
        return Response("not an interaction", status_code=400)
    kind = interaction.get("type")
    if kind == INTERACTION_PING:
        return {"type": RESPONSE_PONG}
    if kind != INTERACTION_COMMAND:
        return Response("unsupported interaction", status_code=400)
    denied = refusal(interaction)
    if denied:
        return reply(f"❌ {denied}")
    command = (interaction.get("data") or {}).get("name")
    if command == RUN:
        return await run_in_threadpool(handle_run, interaction)
    if command == MAU_UPLOAD:
        return handle_mau_upload(interaction, background)
    return reply(f"❌ This relay has no /{command}.")
