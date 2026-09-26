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


class TestGitErrors(unittest.TestCase):
    def test_the_alert_quotes_gits_error_not_a_wrappers_chatter(self):
        from session_ops.shared.git import reason
        stderr = ("2026-09-25 Starting Update of /mirror\nuser@github.com: Permission denied\n"
                  "fatal: Could not read from remote repository.\n")
        self.assertEqual(reason(stderr), "fatal: Could not read from remote repository.")


class TestGitFailures(RepoTest):
    def test_a_rejected_push_quotes_the_servers_refusal(self):
        """GitHub says why in a remote: line; git's own closing error does not."""
        bare = bare_repo(self.root, "app", "main", {"a.txt": "a"})
        hook = os.path.join(bare, "hooks", "pre-receive")
        with open(hook, "w", encoding="utf-8") as handle:
            handle.write("#!/bin/sh\necho 'error: GH006: Protected branch update failed "
                         "for refs/heads/main.' >&2\nexit 1\n")
        os.chmod(hook, 0o755)
        repo = self.clone("app", "main", ["/a.txt"])
        with open(os.path.join(repo.path, "a.txt"), "w", encoding="utf-8") as handle:
            handle.write("b")
        repo.commit("change", AUTHOR)
        with self.assertRaises(RuntimeError) as caught:
            repo.push("main")
        self.assertEqual(str(caught.exception), "git push failed: remote: error: GH006: "
                         "Protected branch update failed for refs/heads/main.")

    def test_a_rejection_without_a_remote_error_quotes_the_rejected_ref(self):
        from session_ops.shared.git import reason
        stderr = ("To https://github.com/o/r\n"
                  " ! [remote rejected] HEAD -> main (pre-receive hook declined)\n"
                  "error: failed to push some refs to 'https://github.com/o/r'\n")
        self.assertEqual(reason(stderr),
                         "! [remote rejected] HEAD -> main (pre-receive hook declined)")

    def test_a_failed_commit_is_named_as_a_commit(self):
        bare_repo(self.root, "app", "main", {"a.txt": "a"})
        repo = self.clone("app", "main", ["/a.txt"])
        with open(os.path.join(repo.path, "a.txt"), "w", encoding="utf-8") as handle:
            handle.write("b")
        with self.assertRaises(RuntimeError) as caught:
            repo.commit("change", " <>")
        self.assertRegex(str(caught.exception), r"^git commit failed: fatal: empty ident")


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

    def test_a_dry_run_shows_the_change_and_pushes_nothing(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            result = publish.pull_request(self.change(), None, "session-foundation/app", "dev",
                                          "bot", "Title", "Body", AUTHOR, True)
        self.assertIn("would push", result)
        self.assertIn("strings.xml | 2 +-", out.getvalue())
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

    def credentials(self, key):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        with open(os.path.join(directory.name, github.APP_KEY_CREDENTIAL), "w",
                  encoding="utf-8") as handle:
            handle.write(key)
        return directory.name

    def publish_token(self, env, api=None):
        signed = []
        def session(token):
            signed.append(token)
            return api
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(github, "session", session):
            return github.publish_token("session-foundation", ["session-ios"]), signed

    def test_the_app_mints_a_token_for_the_named_repositories(self):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()).decode()
        api = FakeSession([FakeResponse({"id": 9}), FakeResponse({"token": "ghs_x"})])
        env = {"CREDENTIALS_DIRECTORY": self.credentials(pem), "GITHUB_APP_ID": "123"}
        token, signed = self.publish_token(env, api)
        self.assertEqual(token, "ghs_x")
        claims = jwt.decode(signed[0], key.public_key(), algorithms=["RS256"])
        self.assertEqual(claims["iss"], "123")
        self.assertTrue(api.calls[0][1].endswith("/orgs/session-foundation/installation"))
        self.assertEqual(api.calls[1][2]["json"], {"repositories": ["session-ios"]})

    def refusal(self, env):
        with self.assertRaises(SystemExit) as caught:
            self.publish_token(env)
        return str(caught.exception)

    def test_a_missing_app_id_is_named(self):
        message = self.refusal({"CREDENTIALS_DIRECTORY": self.credentials("k")})
        self.assertIn("GITHUB_APP_ID is not set", message)
        self.assertNotIn("private key", message)

    def test_an_empty_key_is_named_with_where_it_comes_from(self):
        """install.sh creates the file empty, so the unit starts and this is what says why."""
        message = self.refusal({"CREDENTIALS_DIRECTORY": self.credentials(""),
                                "GITHUB_APP_ID": "123"})
        self.assertIn("private key is missing or empty (/etc/session-ops/github-app.pem",
                      message)
        self.assertNotIn("GITHUB_APP_ID", message)

    def test_nothing_configured_names_both(self):
        message = self.refusal({})
        self.assertIn("GITHUB_APP_ID is not set", message)
        self.assertIn("private key", message)


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
               "PUBLISH_GIT_AUTHOR": AUTHOR}
        api = GitHubFake()
        with mock.patch.object(sync.download_translations_from_crowdin, "main"), \
                mock.patch.object(sync.github, "publish_token", lambda owner, repos: "ghs_x"), \
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


def crowdin_language(lang_id, locale, name):
    return {"id": lang_id, "name": name, "editorCode": lang_id, "twoLettersCode": lang_id,
            "locale": locale, "textDirection": "ltr"}


ENGLISH = crowdin_language("en", "en-US", "English")
GERMAN = crowdin_language("de", "de-DE", "German")


def parsed_translations(path):
    """parse_xliff's output for one target language: a string and a plural."""
    def locale(language, greeting, one, other):
        return {"target_language": language["id"], "language_info": language, "translations": {
            "greeting": {"type": "string", "value": greeting},
            "messageNew": {"type": "plural", "forms": {"one": one, "other": other}}}}
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({
            "source_language": ENGLISH, "target_languages": [GERMAN], "rtl_languages": [],
            "glossary": {"app_name": "Session"},
            "locales": {
                "en-US": locale(ENGLISH, "Hello {name}", "{count} new message",
                                "{count} new messages"),
                "de-DE": locale(GERMAN, "Hallo {name}", "{count} neue Nachricht",
                                "{count} neue Nachrichten")}}, handle)


class TestCrowdinGenerate(RepoTest):
    """The real generators, run by sync into each target's sparse checkout."""

    def setUp(self):
        super().setUp()
        self.parsed = os.path.join(self.work, "parsed_translations.json")
        parsed_translations(self.parsed)

    def generate(self, target, name, branch, files):
        bare_repo(self.root, name, branch, files)
        repo = sync.checkout(target, self.work, None)
        with contextlib.redirect_stdout(io.StringIO()):
            sync.generate(target, repo, self.parsed)
        return repo.path

    def read(self, root, path):
        with open(os.path.join(root, path), encoding="utf-8") as handle:
            return handle.read()

    def test_android_writes_every_locale_and_drops_one_crowdin_no_longer_has(self):
        res = sync.ANDROID_RES
        root = self.generate("android", "session-android", "dev", {
            f"{res}/values/strings.xml": "old", f"{res}/values-b+sh+HR/strings.xml": "stale",
            sync.ANDROID_CONSTANTS: "old"})
        self.assertFalse(os.path.exists(os.path.join(root, res, "values-b+sh+HR/strings.xml")))
        self.assertIn('<string name="app_name" translatable="false">Session</string>',
                      self.read(root, f"{res}/values/strings.xml"))
        german = self.read(root, f"{res}/values-b+de+DE/strings.xml")
        self.assertIn('<string name="greeting">Hallo {name}</string>', german)
        self.assertIn('<item quantity="other">%1$d neue Nachrichten</item>', german)
        self.assertIn('const val APP_NAME = "Session"', self.read(root, sync.ANDROID_CONSTANTS))
        self.assertIn(f" D {res}/values-b+sh+HR/strings.xml",
                      git("status", "--porcelain", cwd=root).split("\n"))

    def test_ios_writes_the_catalog_and_the_constants(self):
        root = self.generate("ios", "session-ios", "dev", {
            f"{sync.IOS_TRANSLATIONS}/Localizable.xcstrings": "old", sync.IOS_CONSTANTS: "old"})
        catalog = json.loads(self.read(root, f"{sync.IOS_TRANSLATIONS}/Localizable.xcstrings"))
        self.assertEqual(catalog["strings"]["greeting"]["localizations"]["de"],
                         {"stringUnit": {"state": "translated", "value": "Hallo {name}"}})
        self.assertIn('public static let app_name: String = "Session"',
                      self.read(root, sync.IOS_CONSTANTS))

    def test_localization_writes_the_modules_and_the_language_list(self):
        root = self.generate("localization", "session-localization", "main",
                             {"generated/english.ts": "old"})
        self.assertEqual(sorted(os.listdir(os.path.join(root, "generated"))),
                         ["constants.ts", "english.ts", "languages.ts", "locales.ts",
                          "translations.ts"])
        self.assertIn("Hello {name}", self.read(root, "generated/english.ts"))
        self.assertIn("Hallo {name}", self.read(root, "generated/translations.ts"))
        self.assertIn("Deutsch", self.read(root, "generated/languages.ts"))


if __name__ == "__main__":
    unittest.main()
