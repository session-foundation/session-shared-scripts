"""
    uv run python -m unittest tests.ops.test_discord_relay

Offline: systemctl, the path units, Discord's CDN and its webhook API are fakes, and
requests go through FastAPI's TestClient. The relay can start jobs on the host, so most
of this is about who it refuses.
"""
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from dataclasses import replace
from datetime import datetime
from unittest import mock

from fastapi.testclient import TestClient
from nacl.signing import SigningKey

from session_ops.ops import discord_commands, discord_relay as relay, registry
from session_ops.platforms import mau
from session_ops.store_versions import job as store_versions
from session_ops.shared.testing import FakeResponse, FakeSession, Patched

KEY = SigningKey.generate()
GUILD = "111"
ALLOWED_USER = "222"
ALLOWED_ROLE = "333"
CHANNEL = "444"
ENV = {"DISCORD_PUBLIC_KEY": KEY.verify_key.encode().hex(), "DISCORD_GUILD_ID": GUILD,
       "ALLOWED_USER_IDS": ALLOWED_USER, "ALLOWED_ROLE_IDS": ALLOWED_ROLE}
EXPORT = f'Date,"{mau.PLAY_COLUMN}"\n"Sep 29, 2026","1,200"\n"Sep 30, 2026","1,250"\n'
CDN_URL = "https://cdn.discordapp.com/attachments/1/2/All%20countries.csv?ex=1"


def fixed_now(*day):
    """A datetime class whose now() is noon on `day`."""
    return type("Fixed", (datetime,), {"now": classmethod(
        lambda cls, tz=None: datetime(*day, 12, tzinfo=tz))})


class FakePathUnits:
    """Each discord job's path unit: a new write to its request starts its unit."""

    def __init__(self):
        self.started, self.picks_up, self.seen = [], True, {}

    def started_at(self, unit):
        name = unit.removeprefix("session-ops@").removesuffix(".service")
        path = os.path.join(registry.REQUESTS_DIR, name)
        if self.picks_up and os.path.exists(path):
            written = os.stat(path).st_mtime_ns
            if self.seen.get(path) != written:
                self.seen[path] = written
                self.started.append(unit)
        return self.started.count(unit) * 1_000_000


class FakeSystemd:
    def __init__(self, running=(), waiting=(), queue_at=None, path_units=None):
        self.running, self.waiting, self.queue_at = set(running), set(waiting), queue_at
        self.path_units = path_units
        self.locked = []

    def __call__(self, *args):
        self.locked.append(relay._starting.locked())
        out = ""
        if args[0] == "list-jobs":
            out = "".join(f"7 {unit} start waiting\n" for unit in self.waiting)
        elif args[0] == "is-active":
            out = "".join(("active" if u in self.running else "inactive") + "\n"
                          for u in args[1:])
        elif args[0] == "show":
            out = f"{self.path_units.started_at(args[-1]) if self.path_units else 0}\n"
        elif args[0] == "list-timers":
            out = json.dumps([] if self.queue_at is None else [
                {"next": int(self.queue_at * 1e6), "unit": args[-1]}])
        return subprocess.CompletedProcess(["systemctl", *args], 0, out, "")


def command(name, options=(), *, user=ALLOWED_USER, roles=(), guild=GUILD, channel=CHANNEL,
            resolved=None):
    data = {"name": name, "options": [{"name": k, "value": v} for k, v in options]}
    if resolved:
        data["resolved"] = resolved
    return {"type": relay.INTERACTION_COMMAND, "id": "999", "application_id": "app",
            "token": "tok", "guild_id": guild, "channel_id": channel, "data": data,
            "member": {"user": {"id": user}, "roles": list(roles)}}


def upload(attachment):
    return command(discord_commands.MAU_UPLOAD, [("file", "55")],
                   resolved={"attachments": {"55": attachment}})


def attachment(**overrides):
    return {"id": "55", "filename": "All countries _ regions.csv", "size": len(EXPORT),
            "url": CDN_URL, **overrides}


def post(body, *, key=KEY, age=0, tamper=False):
    raw = json.dumps(body).encode()
    timestamp = str(int(time.time() - age))
    signature = key.sign(timestamp.encode() + raw).signature.hex()
    if tamper:
        raw = raw.replace(b"}", b" }", 1)
    return TestClient(relay.app).post("/discord/interactions", content=raw, headers={
        "x-signature-ed25519": signature, "x-signature-timestamp": timestamp})


class RelayCase(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, ENV)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.path_units = FakePathUnits()
        self.systemd = FakeSystemd(path_units=self.path_units)
        self.requests = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.requests, True)
        self.patch(registry, REQUESTS_DIR=self.requests)
        self.patch(relay, systemctl=self.systemd, START_WAIT_SECONDS=0.2,
                   START_POLL_SECONDS=0.01)

    @property
    def started(self):
        return self.path_units.started

    def patch(self, module, **attrs):
        patched = Patched(module, **attrs)
        patched.__enter__()
        self.addCleanup(patched.__exit__)

    def content(self, response):
        self.assertEqual(response.status_code, 200)
        return response.json()["data"]["content"]


class TestGate(RelayCase):
    def test_a_ping_is_answered(self):
        self.assertEqual(post({"type": relay.INTERACTION_PING}).json(), {"type": relay.RESPONSE_PONG})

    def test_an_unsigned_tampered_stale_or_foreign_request_is_refused(self):
        for case in ({"tamper": True}, {"age": relay.MAX_SIGNATURE_AGE_SECONDS + 5},
                     {"age": -relay.MAX_CLOCK_SKEW_SECONDS - 5}, {"key": SigningKey.generate()}):
            with self.subTest(**{k: str(v) for k, v in case.items()}):
                self.assertEqual(post({"type": relay.INTERACTION_PING}, **case).status_code, 401)

    def test_no_public_key_refuses_everything(self):
        with mock.patch.dict(os.environ, {"DISCORD_PUBLIC_KEY": ""}):
            self.assertEqual(post({"type": relay.INTERACTION_PING}).status_code, 401)

    def test_another_server_or_a_dm_is_refused(self):
        for guild in ("444", None):
            with self.subTest(guild=guild):
                text = self.content(post(command("run", [("job", "crowdin-sync")], guild=guild)))
                self.assertIn("do not work here", text)
        self.assertEqual(self.started, [])

    def test_someone_on_neither_list_is_refused(self):
        text = self.content(post(command("run", [("job", "crowdin-sync")], user="5", roles=["6"])))
        self.assertIn("not on the list", text)
        self.assertEqual(self.started, [])

    def test_empty_allowlists_refuse_everybody(self):
        with mock.patch.dict(os.environ, {"ALLOWED_USER_IDS": "", "ALLOWED_ROLE_IDS": ""}):
            text = self.content(post(command("run", [("job", "crowdin-sync")])))
        self.assertIn("Neither", text)

    def test_a_role_on_the_list_is_enough(self):
        post(command("run", [("job", "crowdin-sync")], user="5", roles=[ALLOWED_ROLE]))
        self.assertEqual(self.started, ["session-ops@crowdin-sync.service"])

    def test_with_a_channel_list_another_channel_is_refused(self):
        with mock.patch.dict(os.environ, {"ALLOWED_CHANNEL_IDS": f"9, {CHANNEL}"}):
            text = self.content(post(command("run", [("job", "crowdin-sync")], channel="5")))
            self.assertIn("do not work in this channel", text)
            self.assertEqual(self.started, [])
            post(command("run", [("job", "crowdin-sync")]))
        self.assertEqual(self.started, ["session-ops@crowdin-sync.service"])

    def test_without_a_channel_list_any_channel_of_the_server_works(self):
        post(command("run", [("job", "crowdin-sync")], channel="5"))
        self.assertEqual(self.started, ["session-ops@crowdin-sync.service"])


class TestRun(RelayCase):
    def test_a_discord_job_is_started_and_the_channel_told_who_started_it(self):
        response = post(command("run", [("job", "crowdin-duplicates")]))
        self.assertEqual(self.started, ["session-ops@crowdin-duplicates.service"])
        data = response.json()["data"]
        self.assertIn(f"<@{ALLOWED_USER}> started **crowdin-duplicates**", data["content"])
        self.assertNotIn("flags", data)
        self.assertEqual(data["allowed_mentions"], {"parse": []})

    def test_a_job_not_marked_discord_is_refused(self):
        for name in ("token-expiry", "nope"):
            with self.subTest(job=name):
                self.assertIn("not a job /run can start",
                              self.content(post(command("run", [("job", name)]))))
        self.assertEqual(self.started, [])

    def test_a_discord_job_that_is_off_is_refused(self):
        jobs = [replace(job, enabled=job.name != "crowdin-duplicates") for job in registry.load()]
        with Patched(registry, load=lambda *args: jobs):
            self.assertIn("not a job /run can start",
                          self.content(post(command("run", [("job", "crowdin-duplicates")]))))
        self.assertEqual(self.started, [])

    def test_a_running_or_waiting_queue_job_holds_it_off(self):
        queued = registry.load_queue().jobs
        for systemd in (FakeSystemd(running=[f"session-ops@{queued[0]}.service"]),
                        FakeSystemd(waiting=[f"session-ops@{queued[-1]}.service"]),
                        FakeSystemd(running=["session-ops@crowdin-sync.service"])):
            with self.subTest(running=systemd.running, waiting=systemd.waiting), \
                    Patched(relay, systemctl=systemd):
                self.assertIn("running or waiting", self.content(
                    post(command("run", [("job", "crowdin-sync")]))))
        self.assertEqual(self.started, [])

    def test_the_check_and_the_request_happen_under_one_lock(self):
        post(command("run", [("job", "crowdin-sync")]))
        self.assertTrue(self.systemd.locked)
        self.assertTrue(all(self.systemd.locked))
        self.assertFalse(relay._starting.locked())

    def test_each_run_writes_the_request_again_and_nothing_removes_it(self):
        for _ in range(2):
            self.assertIn("started **crowdin-sync**",
                          self.content(post(command("run", [("job", "crowdin-sync")]))))
            time.sleep(0.01)
        self.assertEqual(self.started, ["session-ops@crowdin-sync.service"] * 2)
        self.assertEqual(os.listdir(self.requests), ["crowdin-sync"])

    def test_a_request_nothing_picks_up_is_reported_in_the_reply(self):
        self.path_units.picks_up = False
        data = post(command("run", [("job", "crowdin-sync")])).json()["data"]
        self.assertIn("**crowdin-sync** did not start", data["content"])
        self.assertEqual(data["flags"], relay.EPHEMERAL)

    def test_a_run_that_could_still_be_going_when_the_queue_starts_is_refused(self):
        timeout = registry.get("crowdin-sync").timeout_seconds
        systemd = FakeSystemd(queue_at=time.time() + timeout - 60)
        with Patched(relay, systemctl=systemd):
            text = self.content(post(command("run", [("job", "crowdin-sync")])))
        self.assertIn(f"The queue runs **crowdin-sync** at <t:{int(systemd.queue_at)}:t>", text)
        self.assertEqual(self.started, [])

    def test_a_run_sure_to_end_before_the_queue_starts_goes_ahead(self):
        timeout = registry.get("crowdin-duplicates").timeout_seconds
        systemd = FakeSystemd(queue_at=time.time() + timeout + 60, path_units=self.path_units)
        with Patched(relay, systemctl=systemd):
            post(command("run", [("job", "crowdin-duplicates")]))
        self.assertEqual(self.started, ["session-ops@crowdin-duplicates.service"])

    def test_a_disabled_queue_timer_holds_nothing_off(self):
        post(command("run", [("job", "crowdin-sync")]))
        self.assertEqual(self.started, ["session-ops@crowdin-sync.service"])

    def test_a_missing_request_directory_is_reported_not_raised(self):
        with Patched(registry, REQUESTS_DIR=os.path.join(self.requests, "gone")):
            self.assertIn("Could not ask for the job",
                          self.content(post(command("run", [("job", "crowdin-sync")]))))
        self.assertEqual(self.started, [])

    def test_systemctl_missing_is_reported_not_raised(self):
        def missing(*args):
            raise FileNotFoundError("systemctl")
        with Patched(relay, systemctl=missing):
            self.assertIn("Could not ask for the job",
                          self.content(post(command("run", [("job", "crowdin-sync")]))))


class TestMau(RelayCase):
    def setUp(self):
        super().setUp()
        self.inbox = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.inbox, True)
        self.patch(relay, inbox=lambda: self.inbox, mau_on=lambda: True)

    def request(self):
        with open(os.path.join(self.inbox, mau.REQUEST), encoding="utf-8") as handle:
            return json.load(handle)

    def test_mau_starts_with_whom_to_answer_and_only_the_asker_sees_the_wait(self):
        response = post(command(discord_commands.MAU, [("month", "2026-08")]))
        self.assertEqual(response.json(), {"type": relay.RESPONSE_DEFERRED,
                                           "data": {"flags": relay.EPHEMERAL}})
        self.assertEqual(self.started, ["session-ops@mau.service"])
        self.assertEqual(self.request(), {"application_id": "app", "token": "tok",
                                          "month": "2026-08"})
        self.assertEqual(os.stat(os.path.join(self.inbox, mau.REQUEST)).st_mode & 0o777, 0o644)
        self.assertEqual(os.listdir(self.inbox), [mau.REQUEST])

    def test_without_a_month_mau_answers_for_the_last(self):
        post(command(discord_commands.MAU))
        self.assertIsNone(self.request()["month"])

    def test_a_month_that_has_not_ended_is_refused_before_starting(self):
        for month in ("2099-01", "September"):
            with self.subTest(month=month):
                data = post(command(discord_commands.MAU, [("month", month)])).json()["data"]
                self.assertEqual(data["flags"], relay.EPHEMERAL)
                self.assertTrue(data["content"].startswith("❌ "))
        self.assertEqual((self.started, os.listdir(self.inbox)), ([], []))

    def test_a_request_nothing_picks_up_is_removed(self):
        self.path_units.picks_up = False
        self.assertIn("**mau** did not start",
                      self.content(post(command(discord_commands.MAU))))
        self.assertEqual(os.listdir(self.inbox), [])

    def test_a_busy_queue_holds_it_off_and_writes_nothing(self):
        queued = registry.load_queue().jobs[0]
        with Patched(relay, systemctl=FakeSystemd(running=[f"session-ops@{queued}.service"])):
            self.assertIn("running or waiting", self.content(post(command(discord_commands.MAU))))
        self.assertEqual((self.started, os.listdir(self.inbox)), ([], []))

    def test_run_does_not_start_mau(self):
        self.assertIn("not a job /run can start",
                      self.content(post(command("run", [("job", "mau")]))))
        self.assertEqual(self.started, [])

    def test_while_mau_is_off_it_is_refused(self):
        with Patched(relay, mau_on=lambda: False):
            self.assertIn("mau is off", self.content(post(command(discord_commands.MAU))))
        self.assertEqual(self.started, [])


class TestMauUpload(RelayCase):
    def setUp(self):
        super().setUp()
        self.inbox = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.inbox, True)
        self.cdn = FakeResponse({})
        self.cdn.text = EXPORT
        self.session = FakeSession([self.cdn, FakeResponse({})])
        patched = Patched(relay, inbox=lambda: self.inbox, mau_on=lambda: True)
        patched.__enter__()
        self.addCleanup(patched.__exit__)
        http = Patched(relay.http, Session=lambda **kwargs: self.session)
        http.__enter__()
        self.addCleanup(http.__exit__)

    def told(self):
        method, url, kwargs = self.session.calls[-1]
        self.assertEqual((method, url), ("PATCH", f"{discord_commands.API}/webhooks/app/tok"
                                                  "/messages/@original"))
        return kwargs["json"]["content"]

    def test_an_export_lands_in_the_inbox_under_a_name_mau_reads(self):
        response = post(upload(attachment()))
        self.assertEqual(response.json(), {"type": relay.RESPONSE_DEFERRED,
                                           "data": {"flags": relay.EPHEMERAL}})
        self.assertEqual(os.listdir(self.inbox), ["discord-999.csv"])
        path = os.path.join(self.inbox, "discord-999.csv")
        self.assertEqual(mau.parse_export(path),
                         ("android", {"2026-09-29": 1200, "2026-09-30": 1250}))
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o644)
        self.assertIn("Your Android export, 2 days from 2026-09-29 to 2026-09-30, is in mau's "
                      "inbox. Run `/mau` once", self.told())

    def test_an_export_without_last_month_s_end_says_so(self):
        with Patched(relay, datetime=fixed_now(2026, 11, 3)):
            post(upload(attachment()))
        self.assertIn("no final figure for 31 October, which `/mau` needs for October 2026",
                      self.told())

    def test_an_app_store_connect_export_is_named_ios(self):
        self.cdn.text = ('Name,"Session - Private Messenger"\nStart Date,9/1/26\n'
                         'End Date,9/30/26\n\nDate,Active Last 30 Days\n9/30/26,42000.0\n')
        with Patched(relay, datetime=fixed_now(2026, 10, 3)):
            post(upload(attachment()))
        self.assertIn("iOS export, 1 day from 2026-09-30", self.told())
        self.assertNotIn("no final figure", self.told())

    def test_a_file_mau_would_reject_is_refused_and_leaves_nothing_behind(self):
        self.cdn.text = 'Date,Installs\n"Sep 30, 2026",4\n'
        post(upload(attachment()))
        self.assertEqual(os.listdir(self.inbox), [])
        self.assertIn("not the export mau reads", self.told())

    def test_the_cdn_failing_is_reported(self):
        self.cdn.status_code = 404
        post(upload(attachment()))
        self.assertEqual(os.listdir(self.inbox), [])
        self.assertIn("answered 404", self.told())

    def test_what_cannot_be_an_export_is_refused_before_downloading(self):
        for bad, why in ((attachment(filename="mau.xlsx"), "export a .csv"),
                         (attachment(size=relay.MAX_UPLOAD_BYTES + 1), "larger"),
                         (attachment(url="https://evil.test/a.csv"), "not on Discord's CDN"),
                         (attachment(url=CDN_URL.replace("https", "http")), "not on Discord's CDN"),
                         (None, "no file")):
            with self.subTest(why=why):
                self.assertIn(why, self.content(post(upload(bad))))
        self.assertEqual(self.session.calls, [])
        self.assertEqual(os.listdir(self.inbox), [])

    def test_while_mau_is_off_nothing_is_downloaded(self):
        with Patched(relay, mau_on=lambda: False):
            self.assertIn("mau is off", self.content(post(upload(attachment()))))
        self.assertEqual(self.session.calls, [])
        self.assertEqual(os.listdir(self.inbox), [])

    def test_someone_not_allowed_cannot_upload(self):
        self.assertIn("not on the list", self.content(post(
            {**upload(attachment()), "member": {"user": {"id": "5"}, "roles": []}})))
        self.assertEqual(self.session.calls, [])


def platform_state(**platforms):
    return {"version": store_versions.STATE_VERSION, "platforms": platforms, "sources": {}}


class TestVersions(RelayCase):
    def setUp(self):
        super().setUp()
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder, True)
        os.mkdir(os.path.join(folder, "store-versions"))
        self.state = os.path.join(folder, "store-versions", "state.json")
        self.patch(registry, STATE_ROOT=folder)

    def write(self, state):
        with open(self.state, "w", encoding="utf-8") as handle:
            json.dump(state, handle)

    def test_a_line_per_platform_visible_to_the_channel(self):
        self.write(platform_state(
            ios={"github": "2.15.5", "store": "2.15.5", "store_high": "2.15.6", "warning": True},
            android={"github": "1.33.7", "store": "1.33.7", "warning": False}))
        lines = relay.versions_message().splitlines()
        self.assertEqual(len(lines), len(store_versions.PLATFORMS))
        self.assertIn("**Session iOS**: App Store `2.15.6` / GitHub `2.15.5` ⚠️", lines)
        self.assertIn("**Session Android**: Play Store `1.33.7` / GitHub `1.33.7`", lines)
        self.assertIn("**Session Desktop**: no data yet", lines)

    def test_a_failing_source_says_since_when(self):
        state = platform_state(android={"github": "1.33.7", "store": "1.33.7", "warning": False})
        state["sources"] = {"Play Store (android)": {"since": 1000.0, "alerted_at": None,
                                                     "error": "boom"}}
        self.write(state)
        self.assertIn("**Session Android**: Play Store `1.33.7` / GitHub `1.33.7` "
                      "(not read since <t:1000:R>)", relay.versions_message().splitlines())

    def test_a_non_finite_since_reads_as_unknown(self):
        for since in (float("nan"), float("inf")):
            state = platform_state(android={"github": "1.33.7", "store": "1.33.7",
                                            "warning": False})
            state["sources"] = {"Play Store (android)": {"since": since, "error": "boom"}}
            self.write(state)
            self.assertIn("**Session Android**: Play Store `1.33.7` / GitHub `1.33.7`",
                          relay.versions_message().splitlines())

    def test_a_malformed_platform_reads_as_no_data(self):
        self.write(platform_state(ios="x"))
        self.assertIn("**Session iOS**: no data yet", relay.versions_message().splitlines())

    def test_stale_state_says_so(self):
        self.write(platform_state())
        message = relay.versions_message(self.state, now=time.time() + 3600)
        self.assertIn("may not be running", message)
        self.assertNotIn("may not be running", relay.versions_message(self.state))

    def test_missing_or_foreign_state_is_one_line(self):
        self.assertIn("cannot be read", relay.versions_message(self.state))
        for state in ([], {"version": 99, "platforms": {}}):
            with self.subTest(state=state):
                self.write(state)
                self.assertIn("format", relay.versions_message(self.state))

    def test_anyone_in_the_server_can_ask_but_not_from_elsewhere(self):
        self.write(platform_state())
        with Patched(relay, versions_message=lambda: "the versions"):
            response = post(command(discord_commands.VERSIONS, user="5", roles=["6"]))
            self.assertEqual(response.json()["data"], {"content": "the versions",
                                                       "allowed_mentions": relay.NO_PINGS})
            self.assertIn("do not work here",
                          self.content(post(command(discord_commands.VERSIONS, guild="9"))))
            with mock.patch.dict(os.environ, {"ALLOWED_CHANNEL_IDS": "9"}):
                self.assertIn("do not work in this channel",
                              self.content(post(command(discord_commands.VERSIONS))))


class TestCommands(unittest.TestCase):
    def test_run_offers_the_discord_jobs_but_mau_and_the_job_commands_start_hidden(self):
        jobs = [replace(job, enabled=True) for job in registry.load()]
        run, mau_command, upload_command, versions = discord_commands.commands(jobs)
        offered = [choice["value"] for choice in run["options"][0]["choices"]]
        self.assertEqual(offered, [job.name for job in jobs
                                   if job.discord and job.name != "mau"])
        self.assertIn("crowdin-sync", offered)
        self.assertEqual(mau_command["name"], discord_commands.MAU)
        self.assertFalse(mau_command["options"][0]["required"])
        for item in (run, mau_command, upload_command):
            self.assertEqual(item["default_member_permissions"], "0")
        for choice in run["options"][0]["choices"]:
            self.assertLessEqual(len(choice["name"]), discord_commands.MAX_CHOICE_NAME)
        self.assertEqual(versions["name"], discord_commands.VERSIONS)
        self.assertNotIn("default_member_permissions", versions)

    def test_a_job_that_is_off_is_not_offered_and_mau_off_drops_its_commands(self):
        jobs = [replace(job, enabled=job.name != "crowdin-sync") for job in registry.load()]
        jobs = [replace(job, enabled=False) if job.name == "mau" else job for job in jobs]
        run, versions = discord_commands.commands(jobs)
        self.assertEqual(versions["name"], discord_commands.VERSIONS)
        offered = [choice["value"] for choice in run["options"][0]["choices"]]
        self.assertNotIn("crowdin-sync", offered)
        self.assertIn("crowdin-duplicates", offered)

    def test_registration_replaces_the_guild_commands(self):
        session = FakeSession([FakeResponse([{"name": "run"}, {"name": "mau-upload"}])])
        names = discord_commands.register(session, "app", GUILD, "bot", registry.load())
        method, url, kwargs = session.calls[0]
        self.assertEqual((method, url), ("PUT", f"{discord_commands.API}/applications/app"
                                                f"/guilds/{GUILD}/commands"))
        self.assertEqual(kwargs["headers"], {"Authorization": "Bot bot"})
        self.assertEqual(names, ["run", "mau-upload"])

    def test_a_refused_registration_exits_saying_why(self):
        session = FakeSession([FakeResponse({"message": "Missing Access"}, status_code=403)])
        with self.assertRaises(SystemExit) as raised:
            discord_commands.register(session, "app", GUILD, "bot", registry.load())
        self.assertIn("Missing Access", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
