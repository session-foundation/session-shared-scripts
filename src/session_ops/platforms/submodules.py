"""Pull requests that move a client's submodule to a commit a job just published.

Only the gitlink changes: the client is cloned sparse, and the submodule itself is
never checked out.
"""
import os
from dataclasses import dataclass

from session_ops.platforms import publish
from session_ops.shared.git import Repo


@dataclass(frozen=True)
class Submodule:
    repo: str
    base: str
    path: str


def bump(submodule, sha, work, token, api, branch, title, body, author, dry_run):
    """Publish `submodule` at `sha` to `branch`. Returns a one-line result."""
    repo = Repo.sparse_clone(f"{publish.GITHUB}/{publish.ORG}/{submodule.repo}",
                             submodule.base, os.path.join(work, submodule.repo),
                             ["/.gitmodules"], token)
    repo.set_gitlink(submodule.path, sha)
    return publish.pull_request(repo, api, f"{publish.ORG}/{submodule.repo}", submodule.base,
                                branch, title, body, author, dry_run)
