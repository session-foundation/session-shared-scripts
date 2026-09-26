"""Publishing a checkout's changes as a bot branch and pull request.

The bot branch is rebuilt from the base each run and force-pushed: it belongs to the
job, and nobody commits to it. When the base already matches, the pull request is
closed and the branch deleted, since there is nothing left to merge. A push whose tree
matches the branch already there is skipped, so an unchanged run does not wake the
pull request's reviewers.
"""
from session_ops.shared import github

GITHUB = "https://github.com"
ORG = "session-foundation"


def pull_request(repo, api, name, base, branch, title, body, author, dry_run):
    """Publish `repo`'s uncommitted changes to `branch`. Returns a one-line result."""
    if not repo.changed():
        if not dry_run:
            github.retire_branch(api, name, branch)
        return f"{name}: no changes against {base}"
    repo.commit(title, author)
    if dry_run:
        print(repo.diff_stat())
        return f"{name}: would push {branch} and open a pull request"
    if repo.remote_tree(branch) == repo.tree():
        return f"{name}: {branch} already up to date, " \
               f"{github.ensure_pull(api, name, branch, base, title, body)}"
    repo.push(branch, force=True)
    return f"{name}: {github.ensure_pull(api, name, branch, base, title, body)}"


def direct_push(repo, name, branch, message, author, dry_run):
    """Commit `repo`'s changes straight onto `branch`, without forcing anything."""
    if not repo.changed():
        return f"{name}: no changes on {branch}"
    repo.commit(message, author)
    if dry_run:
        print(repo.diff_stat())
        return f"{name}: would push to {branch}"
    repo.push(branch)
    return f"{name}: pushed to {branch}"
