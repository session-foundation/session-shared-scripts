"""Discord slash commands for session-ops jobs: POST /discord/interactions.

    /run job:<name>   asks for session-ops@<name>.service with a request file, which the
                      job's path unit picks up
    /versions         each platform's store and GitHub versions, from store-versions'
                      last run

Discord signs every interaction with the app's Ed25519 key. One is refused unless it
comes from DISCORD_GUILD_ID, from a channel in ALLOWED_CHANNEL_IDS when that is set, and,
except for the read-only /versions, from someone in ALLOWED_USER_IDS or ALLOWED_ROLE_IDS.

The account this runs as cannot start a unit itself. It can only write files in
/run/session-ops/requests, where a path unit watches for a write to each job jobs.toml
marks `discord`, so a file of any other name starts nothing.

Config (env vars, /etc/session-ops/discord.env):
    DISCORD_PUBLIC_KEY   the app's public key. Unset refuses every request
    DISCORD_GUILD_ID     the one server it answers
    ALLOWED_USER_IDS     comma-separated Discord user ids
    ALLOWED_ROLE_IDS     comma-separated role ids
    ALLOWED_CHANNEL_IDS  (optional) comma-separated channel ids; unset allows any channel

Usage:
    uvicorn session_ops.ops.discord_relay:app --host 127.0.0.1 --port 8081
"""
import json
import math
import os
import subprocess
import threading
import time

from fastapi import FastAPI, Request, Response
from nacl.exceptions import BadSignatureError
from nacl.signing import VerifyKey
from starlette.concurrency import run_in_threadpool

from session_ops.ops import registry
from session_ops.ops.discord_commands import RUN, STORE_VERSIONS_JOB, VERSIONS
from session_ops.store_versions import job as store_versions

# Bounds replay of a request Discord genuinely signed; the signature covers the timestamp.
MAX_SIGNATURE_AGE_SECONDS = 300
# Without it, a host whose clock trails Discord's refuses every interaction, the
# endpoint-registering PING included.
MAX_CLOCK_SKEW_SECONDS = 60
SYSTEMCTL_TIMEOUT_SECONDS = 10
RUNNING_STATES = frozenset({"active", "activating", "deactivating", "reloading"})
USEC_INFINITY = 2**64 - 1
# A path unit reacts within milliseconds; this leaves the rest of Discord's three seconds,
# even to a command that waited on the lock for another's.
START_WAIT_SECONDS = 1.5
START_POLL_SECONDS = 0.1
# store-versions runs every 5 minutes; older state means its timer has stopped.
VERSIONS_STALE_SECONDS = 15 * 60

INTERACTION_PING = 1
INTERACTION_COMMAND = 2
RESPONSE_PONG = 1
RESPONSE_MESSAGE = 4
EPHEMERAL = 64
NO_PINGS = {"parse": []}

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
# Across the check and the start, so two /run a moment apart cannot both pass; one process only.
_starting = threading.Lock()


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


def place_refusal(interaction):
    """Why the commands do not work where this was sent, or None. A DM carries no
    guild_id, so it is refused with the wrong server."""
    guild = env("DISCORD_GUILD_ID")
    if not guild or interaction.get("guild_id") != guild:
        return "These commands do not work here."
    channels = id_list(env("ALLOWED_CHANNEL_IDS"))
    if channels and interaction.get("channel_id") not in channels:
        return "These commands do not work in this channel."
    return None


def refusal(interaction):
    """Why this person may not use the commands here, or None.

    Both allowlists empty refuses everybody: an unconfigured relay must not mean an open one.
    """
    place = place_refusal(interaction)
    if place:
        return place
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


def next_queue_run():
    """The queue timer's next run as a Unix time, or None while it has none.

    From list-timers' JSON, in microseconds: systemd 252 prints this property as local
    time under `show`, --timestamp=unix or not.
    """
    timers = json.loads(systemctl("list-timers", "--all", "--output=json",
                                  registry.QUEUE_TIMER).stdout or "[]")
    next_us = timers[0].get("next") if timers else None
    return next_us / 1e6 if next_us and next_us != USEC_INFINITY else None


def hold_off(job):
    """Why `job` cannot start now, or None."""
    blocking = busy(job)
    if blocking:
        return f"⏳ `{blocking}` is running or waiting to; try again once it has finished."
    queue_at = next_queue_run()
    # A run is killed at its timeout, so the queue starting later cannot overlap it.
    if queue_at is None or queue_at >= time.time() + job.timeout_seconds:
        return None
    at = f"<t:{int(queue_at)}:t> (<t:{int(queue_at)}:R>)"
    if job.queued:
        return (f"⏳ The queue runs **{job.name}** at {at}, sooner than a run started now "
                f"would be sure to end.")
    return (f"⏳ The queue starts at {at}, sooner than **{job.name}** would be sure to end; "
            f"try again once it has run.")


def started_at(unit):
    """When `unit` last left the inactive state, in microseconds since boot; 0 if never."""
    return int(systemctl("show", "-P", "InactiveExitTimestampMonotonic", unit).stdout.strip() or 0)


def request_start(job):
    """Write the job's request, then wait for its path unit to start it. True once it has."""
    unit = unit_name(job.name)
    before = started_at(unit)
    with open(job.request_path, "w"):
        pass
    deadline = time.monotonic() + START_WAIT_SECONDS
    while time.monotonic() < deadline:
        if started_at(unit) != before:
            return True
        time.sleep(START_POLL_SECONDS)
    return False


def start(job, interaction):
    """None once `job` has started, else the reply saying why not."""
    command = (interaction.get("data") or {}).get("name")
    # Waiting out another start's poll would miss Discord's 3 s.
    if not _starting.acquire(blocking=False):
        return reply("⏳ Another job is being started; try again in a moment.")
    try:
        held = hold_off(job)
        if held:
            return reply(held)
        started = request_start(job)
    except (OSError, subprocess.SubprocessError, RuntimeError, ValueError) as exc:
        print(f"/{command} {job.name}: {exc!r}", flush=True)
        return reply("❌ Could not ask for the job on the host; see "
                     "`journalctl -u session-ops-discord`.")
    finally:
        _starting.release()
    if not started:
        print(f"/{command} {job.name} by {user_id(interaction)}: nothing picked the request up",
              flush=True)
        return reply(f"❌ **{job.name}** did not start: nothing picked up its request. See "
                     f"`systemctl status session-ops@{job.name}.path session-ops@{job.name}`.")
    print(f"/{command} {job.name} by {user_id(interaction)}: started", flush=True)
    return None


def handle_run(interaction):
    name = option(interaction, "job")
    job = next((job for job in registry.load() if job.discord and job.name == name), None)
    if job is None:
        return reply(f"❌ `{name}` is not a job /run can start.")
    return start(job, interaction) or reply(
        f"▶️ <@{user_id(interaction)}> started **{job.name}**. It posts in its own channel "
        "when it is done.", ephemeral=False)


def versions_message(path=None, now=None):
    """divemo's /versions: a line per platform from store-versions' state."""
    path = path or os.path.join(registry.get(STORE_VERSIONS_JOB).state_dir, "state.json")
    try:
        with open(path, encoding="utf-8") as handle:
            state = json.load(handle)
        written = os.stat(path).st_mtime
    except (OSError, ValueError) as exc:
        print(f"/versions: {exc!r}", flush=True)
        return "❌ No versions to show: store-versions' state cannot be read on the host."
    seen = state.get("platforms") if isinstance(state, dict) else None
    if not isinstance(seen, dict) or state.get("version") != store_versions.STATE_VERSION:
        return "❌ No versions to show: store-versions' state is in a format this cannot read."
    failing = state.get("sources")
    failing = failing if isinstance(failing, dict) else {}
    lines = []
    for platform in store_versions.PLATFORMS:
        last = seen.get(platform.name)
        if not isinstance(last, dict) or not last:
            lines.append(f"**{platform.display}**: no data yet")
            continue
        # The watermark, since that is what the warnings compare.
        store = last.get("store_high") or last.get("store")
        line = (f"**{platform.display}**: {store_versions.store_name(platform)} "
                f"`{store}` / GitHub `{last.get('github')}`"
                + (" ⚠️" if last.get("warning") else ""))
        # A failing source leaves the platform's last good versions in place.
        since = [f.get("since") for key in store_versions.source_keys(platform)
                 if isinstance(f := failing.get(key), dict)
                 and isinstance(f.get("since"), (int, float)) and math.isfinite(f["since"])]
        if since:
            line += f" (not read since <t:{int(min(since))}:R>)"
        lines.append(line)
    if (now or time.time()) - written > VERSIONS_STALE_SECONDS:
        lines.append(f"-# As of <t:{int(written)}:R>: store-versions may not be running.")
    return "\n".join(lines)


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.post("/discord/interactions")
async def interactions(request: Request):
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
    command = (interaction.get("data") or {}).get("name")
    denied = place_refusal(interaction) if command == VERSIONS else refusal(interaction)
    if denied:
        return reply(f"❌ {denied}")
    if command == VERSIONS:
        return reply(await run_in_threadpool(versions_message), ephemeral=False)
    if command == RUN:
        return await run_in_threadpool(handle_run, interaction)
    return reply(f"❌ This relay has no /{command}.")
