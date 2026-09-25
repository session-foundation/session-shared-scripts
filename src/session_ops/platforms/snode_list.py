"""
Daily: copy the service node list from session-desktop-dynamic-assets into session-ios,
as the fallback a new client uses when it cannot reach the seed nodes, and open a pull
request from feature/update-static-snode-list when it changed.

The list is published as fetched, byte for byte, but only once it parses as JSON: a
fallback that fails to parse would strand exactly the clients it exists for.

    session-ops run snode-list [--dry-run]
"""
import argparse
import json
import os
import tempfile

from session_ops.ops.runner import step
from session_ops.platforms import publish
from session_ops.shared import github, http
from session_ops.shared.git import Repo

SOURCE = ("https://raw.githubusercontent.com/session-foundation/"
          "session-desktop-dynamic-assets/main/service-nodes-cache.json")
REPO = "session-ios"
PATH = "Session/Meta/service-nodes-cache.json"
BRANCH = "feature/update-static-snode-list"
TITLE = "[Automated] Update fallback static snode list"
BODY = """[Automated]
This PR updates the static service node list which is used as a fallback when a new client is unable to contact the seed nodes
"""


def fetch(session):
    resp = session.request("GET", SOURCE)
    if resp.status_code != 200:
        raise RuntimeError(f"{SOURCE} answered {resp.status_code}")
    try:
        json.loads(resp.content)
    except ValueError as exc:
        raise RuntimeError(f"{SOURCE} is not JSON: {exc}") from exc
    return resp.content


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.strip().split("\n")[0])
    parser.add_argument("--dry-run", action="store_true",
                        help="Commit locally and show the change; push nothing.")
    args = parser.parse_args(argv)

    author = os.environ.get("PUBLISH_GIT_AUTHOR") or "session-ops <session-ops@localhost>"
    work = os.environ.get("SESSION_OPS_WORK_DIR") or tempfile.mkdtemp(prefix="snode-list-")
    step("fetch")
    content = fetch(http.Session())
    token = None if args.dry_run else github.publish_token(publish.ORG, [REPO])
    step("checkout")
    repo = Repo.sparse_clone(f"{publish.GITHUB}/{publish.ORG}/{REPO}", "dev",
                             os.path.join(work, REPO), [f"/{PATH}"], token)
    with open(os.path.join(repo.path, PATH), "wb") as handle:
        handle.write(content)
    step("publish")
    print(publish.pull_request(repo, github.session(token) if token else None,
                               f"{publish.ORG}/{REPO}", "dev", BRANCH, TITLE, BODY, author,
                               args.dry_run))
