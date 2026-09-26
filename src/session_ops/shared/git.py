"""The git a publishing job needs: a shallow, sparse checkout of just the paths it
writes, a commit, and a push.

The token travels in the environment as an http.extraHeader, never on a command
line, where any account on the box could read it out of `ps`.
"""
import base64
import os
import subprocess


def reason(stderr):
    """git's own error line, rather than whatever a wrapper printed before it.

    On a rejected push the server's refusal says why; git's closing
    "error: failed to push some refs" does not.
    """
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    for prefixes in (("remote: error:",), ("! [",), ("fatal:", "error:")):
        errors = [line for line in lines if line.startswith(prefixes)]
        if errors:
            return errors[0][:300]
    return (lines or ["no output"])[0][:300]


def subcommand(args):
    """The git command `args` run, past any leading `-c name=value` pairs."""
    index = 0
    while index < len(args) and args[index] == "-c":
        index += 2
    return args[index] if index < len(args) else "git"


class Repo:
    def __init__(self, path, token=None):
        self.path = path
        self.env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
        if token:
            basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
            self.env.update({"GIT_CONFIG_COUNT": "1",
                             "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
                             "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {basic}"})

    def git(self, *args, check=True):
        done = subprocess.run(["git", "-C", self.path, *args], env=self.env,
                              capture_output=True, text=True, check=False)
        if check and done.returncode:
            raise RuntimeError(f"git {subcommand(args)} failed: {reason(done.stderr)}")
        return done

    @classmethod
    def sparse_clone(cls, url, branch, path, patterns, token=None, blob_filter=True):
        """Check out only `patterns` (gitignore syntax) of `branch`'s tip."""
        repo = cls(os.path.dirname(path) or ".", token)
        repo.git("clone", "--depth", "1", "--no-checkout", "--branch", branch,
                 *(["--filter=blob:none"] if blob_filter else []), url, path)
        repo.path = path
        repo.git("sparse-checkout", "set", "--no-cone", *patterns)
        repo.git("checkout", branch)
        return repo

    def changed(self):
        return bool(self.git("status", "--porcelain").stdout.strip())

    def diff_stat(self):
        """What the last commit changed."""
        return self.git("show", "--stat", "--format=", "HEAD").stdout

    def commit(self, message, author):
        """Commit everything in the checkout as `author` ("Name <email>")."""
        name, _, email = author.partition(" <")
        self.git("add", "--all")
        self.git("-c", f"user.name={name}", "-c", f"user.email={email.rstrip('>')}",
                 "commit", "--quiet", "--message", message)

    def remote_tree(self, branch):
        """The tree at the remote `branch`, or None when it does not exist."""
        fetched = self.git("fetch", "--quiet", "--depth", "1", "origin",
                           f"refs/heads/{branch}", check=False)
        if fetched.returncode:
            return None
        return self.git("rev-parse", "FETCH_HEAD^{tree}").stdout.strip()

    def tree(self):
        return self.git("rev-parse", "HEAD^{tree}").stdout.strip()

    def push(self, branch, force=False):
        self.git("push", *(["--force"] if force else []), "origin", f"HEAD:refs/heads/{branch}")
