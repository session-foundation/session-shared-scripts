"""Publishing a checkout's changes as a bot branch and pull request.

The bot branch is rebuilt from the base each run and force-pushed: it belongs to the
job, and nobody commits to it. When the base already matches, the pull request is
closed and the branch deleted, since there is nothing left to merge. A push whose tree
matches the branch already there is skipped, so an unchanged run does not wake the
pull request's reviewers.
"""
from session_ops.shared import github
from session_ops.shared.env import rehearsing

GITHUB = "https://github.com"
ORG = github.ORG
REHEARSAL_PREFIX = github.REHEARSAL_PREFIX
REHEARSAL_NOTE = ("**Rehearsal of session-ops: do not merge.** Close it and delete the branch "
                  "once reviewed; production publishes to the branch without the "
                  f"`{REHEARSAL_PREFIX}` prefix.\n\n")


def pull_request(repo, api, name, base, branch, title, body, author, dry_run):
    """Publish `repo`'s uncommitted changes to `branch`. Returns a one-line result."""
    if rehearsing():
        branch, title, body = REHEARSAL_PREFIX + branch, f"[Rehearsal] {title}", REHEARSAL_NOTE + body
    # Before anything else, so a dry run catches a branch the App may not write.
    github.require_publishable(name, branch, force=True)
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


def direct_push(repo, api, name, branch, message, body, author, dry_run):
    """Commit `repo`'s changes straight onto `branch`, without forcing anything. A
    rehearsal opens a pull request against `branch` instead."""
    if rehearsing():
        return pull_request(repo, api, name, branch, f"direct-push-to-{branch}", message, body,
                            author, dry_run)
    github.require_publishable(name, branch)
    if not repo.changed():
        return f"{name}: no changes on {branch}"
    repo.commit(message, author)
    if dry_run:
        print(repo.diff_stat())
        return f"{name}: would push to {branch}"
    repo.push(branch)
    return f"{name}: pushed to {branch}"
