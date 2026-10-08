"""
    uv run python -m unittest tests.ops.test_discord_relay

Offline: systemctl, Discord's CDN and its webhook API are fakes, and requests go
through FastAPI's TestClient. The relay can start jobs on the host, so most of this is
about who it refuses.
"""
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from fastapi.testclient import TestClient
from nacl.signing import SigningKey

from session_ops.ops import discord_commands, discord_relay as relay, registry
from session_ops.platforms import mau
from session_ops.shared.testing import FakeResponse, FakeSession, Patched

KEY = SigningKey.generate()
GUILD = "111"
ALLOWED_USER = "222"
ALLOWED_ROLE = "333"
ENV = {"DISCORD_PUBLIC_KEY": KEY.verify_key.encode().hex(), "DISCORD_GUILD_ID": GUILD,
       "ALLOWED_USER_IDS": ALLOWED_USER, "ALLOWED_ROLE_IDS": ALLOWED_ROLE}
EXPORT = f'Date,"{mau.MAU_COLUMN}"\n"Sep 29, 2026","1,200"\n"Sep 30, 2026","1,250"\n'
CDN_URL = "https://cdn.discordapp.com/attachments/1/2/All%20countries.csv?ex=1"


class FakeSystemd:
    def __init__(self, running=(), waiting=(), start_error=None, queue_at=None):
        self.running, self.waiting, self.start_error = set(running), set(waiting), start_error
        self.queue_at = queue_at
        self.started, self.locked = [], []

    def __call__(self, *args):
        out, code, err = "", 0, ""
        if args[0] == "list-jobs":
            out = "".join(f"7 {unit} start waiting\n" for unit in self.waiting)
        elif args[0] == "is-active":
            out = "".join(("active" if u in self.running else "inactive") + "\n"
                          for u in args[1:])
        elif args[0] == "list-timers":
            out = json.dumps([] if self.queue_at is None else [
                {"next": int(self.queue_at * 1e6), "unit": args[-1]}])
        elif args[0] == "start":
            self.started.append(args[-1])
            self.locked.append(relay._starting.locked())
            code, err = (1, self.start_error) if self.start_error else (0, "")
        return subprocess.CompletedProcess(["systemctl", *args], code, out, err)


def command(name, options=(), *, user=ALLOWED_USER, roles=(), guild=GUILD, resolved=None):
    data = {"name": name, "options": [{"name": k, "value": v} for k, v in options]}
    if resolved:
        data["resolved"] = resolved
    return {"type": relay.INTERACTION_COMMAND, "id": "999", "application_id": "app",
            "token": "tok", "guild_id": guild, "data": data,
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
        self.systemd = FakeSystemd()
        patched = Patched(relay, systemctl=self.systemd)
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
        self.assertEqual(self.systemd.started, [])

    def test_someone_on_neither_list_is_refused(self):
        text = self.content(post(command("run", [("job", "crowdin-sync")], user="5", roles=["6"])))
        self.assertIn("not on the list", text)
        self.assertEqual(self.systemd.started, [])

    def test_empty_allowlists_refuse_everybody(self):
        with mock.patch.dict(os.environ, {"ALLOWED_USER_IDS": "", "ALLOWED_ROLE_IDS": ""}):
            text = self.content(post(command("run", [("job", "crowdin-sync")])))
        self.assertIn("Neither", text)

    def test_a_role_on_the_list_is_enough(self):
        post(command("run", [("job", "crowdin-sync")], user="5", roles=[ALLOWED_ROLE]))
        self.assertEqual(self.systemd.started, ["session-ops@crowdin-sync.service"])


class TestRun(RelayCase):
    def test_a_discord_job_is_started_and_the_channel_told_who_started_it(self):
        response = post(command("run", [("job", "crowdin-duplicates")]))
        self.assertEqual(self.systemd.started, ["session-ops@crowdin-duplicates.service"])
        data = response.json()["data"]
        self.assertIn(f"<@{ALLOWED_USER}> started **crowdin-duplicates**", data["content"])
        self.assertNotIn("flags", data)
        self.assertEqual(data["allowed_mentions"], {"parse": []})

    def test_a_job_not_marked_discord_is_refused(self):
        for name in ("token-expiry", "nope"):
            with self.subTest(job=name):
                self.assertIn("not a job /run can start",
                              self.content(post(command("run", [("job", name)]))))
        self.assertEqual(self.systemd.started, [])

    def test_a_running_or_waiting_queue_job_holds_it_off(self):
        queued = registry.load_queue().jobs
        for systemd in (FakeSystemd(running=[f"session-ops@{queued[0]}.service"]),
                        FakeSystemd(waiting=[f"session-ops@{queued[-1]}.service"]),
                        FakeSystemd(running=["session-ops@crowdin-sync.service"])):
            with self.subTest(running=systemd.running, waiting=systemd.waiting), \
                    Patched(relay, systemctl=systemd):
                self.assertIn("running or waiting", self.content(
                    post(command("run", [("job", "crowdin-sync")]))))
                self.assertEqual(systemd.started, [])

    def test_the_check_and_the_start_happen_under_one_lock(self):
        post(command("run", [("job", "crowdin-sync")]))
        self.assertEqual(self.systemd.locked, [True])
        self.assertFalse(relay._starting.locked())

    def test_a_run_that_could_still_be_going_when_the_queue_starts_is_refused(self):
        timeout = registry.get("crowdin-sync").timeout_seconds
        systemd = FakeSystemd(queue_at=time.time() + timeout - 60)
        with Patched(relay, systemctl=systemd):
            text = self.content(post(command("run", [("job", "crowdin-sync")])))
        self.assertIn(f"The queue runs **crowdin-sync** at <t:{int(systemd.queue_at)}:t>", text)
        self.assertEqual(systemd.started, [])

    def test_a_run_sure_to_end_before_the_queue_starts_goes_ahead(self):
        timeout = registry.get("crowdin-duplicates").timeout_seconds
        systemd = FakeSystemd(queue_at=time.time() + timeout + 60)
        with Patched(relay, systemctl=systemd):
            post(command("run", [("job", "crowdin-duplicates")]))
        self.assertEqual(systemd.started, ["session-ops@crowdin-duplicates.service"])

    def test_a_disabled_queue_timer_holds_nothing_off(self):
        post(command("run", [("job", "crowdin-sync")]))
        self.assertEqual(self.systemd.started, ["session-ops@crowdin-sync.service"])

    def test_a_refusal_from_systemd_is_passed_on(self):
        systemd = FakeSystemd(start_error="Failed to start: Access denied")
        with Patched(relay, systemctl=systemd):
            text = self.content(post(command("run", [("job", "crowdin-sync")])))
        self.assertIn("Access denied", text)

    def test_systemctl_missing_is_reported_not_raised(self):
        def missing(*args):
            raise FileNotFoundError("systemctl")
        with Patched(relay, systemctl=missing):
            self.assertIn("Could not ask systemd",
                          self.content(post(command("run", [("job", "crowdin-sync")]))))


class TestMauUpload(RelayCase):
    def setUp(self):
        super().setUp()
        self.inbox = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.inbox, True)
        self.cdn = FakeResponse({})
        self.cdn.text = EXPORT
        self.session = FakeSession([self.cdn, FakeResponse({})])
        patched = Patched(relay, inbox=lambda: self.inbox)
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
        self.assertEqual(response.json(), {"type": relay.RESPONSE_DEFERRED})
        self.assertEqual(os.listdir(self.inbox), ["discord-999.csv"])
        path = os.path.join(self.inbox, "discord-999.csv")
        self.assertEqual(mau.parse_export(path), {"2026-09-29": 1200, "2026-09-30": 1250})
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o644)
        self.assertIn("2 days from 2026-09-29 to 2026-09-30", self.told())

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
        for bad, why in ((attachment(filename="mau.xlsx"), "exports a .csv"),
                         (attachment(size=relay.MAX_UPLOAD_BYTES + 1), "larger"),
                         (attachment(url="https://evil.test/a.csv"), "not on Discord's CDN"),
                         (attachment(url=CDN_URL.replace("https", "http")), "not on Discord's CDN"),
                         (None, "no file")):
            with self.subTest(why=why):
                self.assertIn(why, self.content(post(upload(bad))))
        self.assertEqual(self.session.calls, [])
        self.assertEqual(os.listdir(self.inbox), [])

    def test_someone_not_allowed_cannot_upload(self):
        self.assertIn("not on the list", self.content(post(
            {**upload(attachment()), "member": {"user": {"id": "5"}, "roles": []}})))
        self.assertEqual(self.session.calls, [])


class TestCommands(unittest.TestCase):
    def test_run_offers_exactly_the_discord_jobs_and_both_start_hidden(self):
        jobs = registry.load()
        run, upload_command = discord_commands.commands(jobs)
        offered = [choice["value"] for choice in run["options"][0]["choices"]]
        self.assertEqual(offered, [job.name for job in jobs if job.discord])
        self.assertIn("crowdin-sync", offered)
        for item in (run, upload_command):
            self.assertEqual(item["default_member_permissions"], "0")
        for choice in run["options"][0]["choices"]:
            self.assertLessEqual(len(choice["name"]), discord_commands.MAX_CHOICE_NAME)

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
