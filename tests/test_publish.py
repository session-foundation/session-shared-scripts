"""
    uv run python -m unittest tests.test_publish

Real git against bare repositories on disk standing in for GitHub; the REST calls go
to a fake.
"""
import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from unittest import mock

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from session_ops.crowdin import sync
from session_ops.platforms import publish, snode_list
from session_ops.shared import github
from session_ops.shared.git import Repo
from session_ops.shared.testing import FakeResponse, FakeSession

AUTHOR = "Bot <bot@example.org>"


def git(*args, cwd=None):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout.strip()


def bare_repo(root, name, branch, files):
    """A bare repository `name` whose `branch` holds `files`."""
    seed = os.path.join(root, f"seed-{name}")
    os.makedirs(seed)
    git("init", "-q", "-b", branch, cwd=seed)
    for path, content in files.items():
        os.makedirs(os.path.dirname(os.path.join(seed, path)) or seed, exist_ok=True)
        with open(os.path.join(seed, path), "w", encoding="utf-8") as handle:
            handle.write(content)
    git("add", "-A", cwd=seed)
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "seed", cwd=seed)
    bare = os.path.join(root, "session-foundation", name)
    git("clone", "-q", "--bare", seed, bare)
    git("config", "uploadpack.allowfilter", "true", cwd=bare)
    return bare


class GitHubFake(FakeSession):
    """Answers the pull-request calls publish makes, from an in-memory list."""

    def __init__(self, open_pulls=()):
        super().__init__([])
        self.pulls = list(open_pulls)

    def request(self, method, url, params=None, json=None, **kwargs):
        self.calls.append((method, url, {"params": params, "json": json}))
        if method == "GET" and url.endswith("/pulls"):
            return FakeResponse(self.pulls)
        if method == "POST" and url.endswith("/pulls"):
            return FakeResponse({"number": 7, "html_url": "https://github.com/pr/7"})
        if method == "PATCH":
            return FakeResponse({"html_url": "https://github.com/pr/5"})
        if method == "DELETE":
            return FakeResponse({}, status_code=204)
        raise AssertionError((method, url))


class RepoTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.work = tempfile.mkdtemp()
        patcher = mock.patch.object(publish, "GITHUB", f"file://{self.root}")
        patcher.start()
        self.addCleanup(patcher.stop)

    def clone(self, name, branch, patterns):
        return Repo.sparse_clone(f"file://{self.root}/session-foundation/{name}", branch,
                                 tempfile.mkdtemp(dir=self.work) + f"/{name}", patterns)

    def branches(self, name):
        return git("for-each-ref", "--format=%(refname:short)", "refs/heads",
                   cwd=os.path.join(self.root, "session-foundation", name)).split()


class TestSparseCheckout(RepoTest):
    def test_only_the_named_paths_are_checked_out(self):
        bare_repo(self.root, "app", "dev", {"res/values/strings.xml": "a", "src/Main.kt": "b"})
        repo = self.clone("app", "dev", ["/res/values*/strings.xml"])
        self.assertTrue(os.path.exists(os.path.join(repo.path, "res/values/strings.xml")))
        self.assertFalse(os.path.exists(os.path.join(repo.path, "src/Main.kt")))
        self.assertFalse(repo.changed())

    def test_the_token_travels_in_the_environment_not_the_command_line(self):
        """Any account on the box can read another process's argv from `ps`."""
        import base64
        with mock.patch("session_ops.shared.git.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, "", "")
            Repo("/tmp", token="ghs_secret").push("bot", force=True)
        argv, env = run.call_args.args[0], run.call_args.kwargs["env"]
        self.assertNotIn("ghs_secret", " ".join(argv))
        header = env["GIT_CONFIG_VALUE_0"].split()[-1]
        self.assertEqual(base64.b64decode(header), b"x-access-token:ghs_secret")


class TestPullRequest(RepoTest):
    def setUp(self):
        super().setUp()
        bare_repo(self.root, "app", "dev", {"strings.xml": "old\n", "other.txt": "x"})

    def change(self, content="new\n"):
        repo = self.clone("app", "dev", ["/strings.xml"])
        with open(os.path.join(repo.path, "strings.xml"), "w", encoding="utf-8") as handle:
            handle.write(content)
        return repo

    def publish(self, repo, api, dry_run=False):
        with contextlib.redirect_stdout(io.StringIO()):
            return publish.pull_request(repo, api, "session-foundation/app", "dev", "bot",
                                        "Title", "Body", AUTHOR, dry_run)

    def test_a_change_is_force_pushed_to_the_bot_branch_and_a_pull_request_opened(self):
        api = GitHubFake()
        result = self.publish(self.change(), api)
        self.assertIn("https://github.com/pr/7", result)
        self.assertIn("bot", self.branches("app"))
        opened = [c for c in api.calls if c[0] == "POST"][0][2]["json"]
        self.assertEqual((opened["head"], opened["base"]), ("bot", "dev"))
        author = git("log", "-1", "--format=%an <%ae>", "bot",
                     cwd=os.path.join(self.root, "session-foundation", "app"))
        self.assertEqual(author, AUTHOR)

    def test_an_open_pull_request_is_updated_rather_than_duplicated(self):
        api = GitHubFake(open_pulls=[{"number": 5, "html_url": "https://github.com/pr/5"}])
        self.publish(self.change(), api)
        self.assertEqual([c[0] for c in api.calls if c[0] in ("POST", "PATCH")], ["PATCH"])

    def test_the_same_tree_twice_is_not_pushed_again(self):
        self.publish(self.change(), GitHubFake())
        tip = git("rev-parse", "bot", cwd=os.path.join(self.root, "session-foundation", "app"))
        result = self.publish(self.change(), GitHubFake())
        self.assertIn("already up to date", result)
        self.assertEqual(tip, git("rev-parse", "bot",
                                  cwd=os.path.join(self.root, "session-foundation", "app")))

    def test_no_change_retires_the_branch_and_its_pull_request(self):
        api = GitHubFake(open_pulls=[{"number": 5, "html_url": "https://github.com/pr/5"}])
        result = self.publish(self.change("old\n"), api)
        self.assertIn("no changes", result)
        self.assertEqual([c[0] for c in api.calls], ["GET", "PATCH", "DELETE"])
        self.assertEqual(api.calls[1][2]["json"], {"state": "closed"})

    def test_a_dry_run_pushes_nothing_and_calls_no_api(self):
        result = self.publish(self.change(), None, dry_run=True)
        self.assertIn("would push", result)
        self.assertEqual(self.branches("app"), ["dev"])


class TestDirectPush(RepoTest):
    def test_a_change_lands_on_the_branch_without_force(self):
        bare_repo(self.root, "module", "main", {"generated/english.ts": "a"})
        repo = self.clone("module", "main", ["/generated/"])
        with open(os.path.join(repo.path, "generated/english.ts"), "w") as handle:
            handle.write("b")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertIn("pushed", publish.direct_push(repo, "m", "main", "T", AUTHOR, False))
        self.assertEqual(git("show", "main:generated/english.ts",
                             cwd=os.path.join(self.root, "session-foundation", "module")), "b")


class TestAppToken(unittest.TestCase):
    def test_the_jwt_is_signed_by_the_app_and_short_lived(self):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()).decode()
        claims = jwt.decode(github.app_jwt(123, pem, now=1_000_000), key.public_key(),
                            algorithms=["RS256"], options={"verify_exp": False})
        self.assertEqual((claims["iss"], claims["exp"] - claims["iat"]), ("123", 600))

    def test_the_installation_token_is_scoped_to_the_named_repositories(self):
        api = FakeSession([FakeResponse({"id": 9}), FakeResponse({"token": "ghs_x"})])
        self.assertEqual(github.installation_token(1, "k", "session-foundation",
                                                   ["session-ios"], api=api), "ghs_x")
        self.assertTrue(api.calls[1][1].endswith("/app/installations/9/access_tokens"))
        self.assertEqual(api.calls[1][2]["json"], {"repositories": ["session-ios"]})

    def test_an_empty_key_file_means_no_app(self):
        directory = tempfile.mkdtemp()
        open(os.path.join(directory, github.APP_KEY_CREDENTIAL), "w").close()
        env = {"CREDENTIALS_DIRECTORY": directory, "GITHUB_APP_ID": "1",
               "GITHUB_PUBLISH_TOKEN": "pat"}
        with mock.patch.dict(os.environ, env):
            self.assertEqual(github.publish_token("o", ["r"]), "pat")

    def test_no_way_to_publish_is_refused(self):
        with mock.patch.dict(os.environ, {"GITHUB_PUBLISH_TOKEN": "", "GITHUB_APP_ID": ""}), \
                self.assertRaises(SystemExit):
            github.publish_token("o", ["r"])


class TestSnodeList(RepoTest):
    def run_job(self, body, *argv):
        bare_repo(self.root, "session-ios", "dev",
                  {snode_list.PATH: '{"old": true}\n', "Other.swift": "x"})
        fetched = FakeSession([FakeResponse(None)])
        fetched._responses[0].text = body
        with mock.patch.object(snode_list.http, "Session", lambda: fetched), \
                mock.patch.dict(os.environ, {"SESSION_OPS_WORK_DIR": self.work}), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            snode_list.main(list(argv))
        return out.getvalue()

    def test_the_fetched_list_is_committed_byte_for_byte(self):
        body = '{"service_node_states": [1, 2]}'
        self.assertIn("would push", self.run_job(body, "--dry-run"))
        with open(os.path.join(self.work, "session-ios", snode_list.PATH),
                  encoding="utf-8") as handle:
            self.assertEqual(handle.read(), body)

    def test_a_body_that_is_not_json_is_never_published(self):
        with self.assertRaises(RuntimeError):
            self.run_job("<html>rate limited</html>", "--dry-run")


class TestCrowdinSync(RepoTest):
    """The orchestration: every target checked out, generated and published, and one
    target's failure recorded without stopping the others."""

    def setUp(self):
        super().setUp()
        bare_repo(self.root, "session-android", "dev",
                  {f"{sync.ANDROID_RES}/values/strings.xml": "old", sync.ANDROID_CONSTANTS: "k"})
        bare_repo(self.root, "session-ios", "dev",
                  {f"{sync.IOS_TRANSLATIONS}/Localizable.xcstrings": "old",
                   sync.IOS_CONSTANTS: "k"})
        bare_repo(self.root, "session-localization", "main", {"generated/english.ts": "old"})

    def fake_generate(self, target, repo, parsed):
        if target == "ios":
            raise RuntimeError("catalog write failed")
        path = {"android": f"{sync.ANDROID_RES}/values-de/strings.xml",
                "localization": "generated/english.ts"}[target]
        os.makedirs(os.path.dirname(os.path.join(repo.path, path)), exist_ok=True)
        with open(os.path.join(repo.path, path), "w", encoding="utf-8") as handle:
            handle.write(json.dumps(target))

    def test_each_target_publishes_on_its_own(self):
        env = {"SESSION_OPS_WORK_DIR": self.work, "CROWDIN_API_TOKEN": "t",
               "PUBLISH_GIT_AUTHOR": AUTHOR, "GITHUB_PUBLISH_TOKEN": "pat"}
        api = GitHubFake()
        with mock.patch.object(sync.download_translations_from_crowdin, "main"), \
                mock.patch.object(sync.parse_xliff, "main"), \
                mock.patch.object(sync, "generate", self.fake_generate), \
                mock.patch.object(sync.github, "session", lambda token: api), \
                mock.patch.dict(os.environ, env), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            outcome = sync.main([])
        self.assertEqual(outcome.failures(), {"ios": "RuntimeError: catalog write failed"})
        self.assertIn(sync.BOT_BRANCH, self.branches("session-android"))
        self.assertEqual(git("show", "main:generated/english.ts",
                             cwd=os.path.join(self.root, "session-foundation",
                                              "session-localization")), '"localization"')
        opened = [c for c in api.calls if c[0] == "POST"]
        self.assertEqual(len(opened), 1)
        self.assertEqual(opened[0][2]["json"]["title"], sync.TITLE)


if __name__ == "__main__":
    unittest.main()
