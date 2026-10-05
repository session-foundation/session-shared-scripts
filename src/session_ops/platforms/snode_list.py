"""
Weekdays: fetch the service node list from the seed nodes and publish it wherever a
client bundles it, as the fallback a new client uses when it cannot reach them.

- session-desktop-dynamic-assets gets it as a commit straight onto `main`.
- session-desktop gets a pull request from update-dynamic-assets moving its
  dynamic_assets submodule to that commit.
- session-ios gets a copy in a pull request from feature/update-static-snode-list.

Each target is independent, except that Desktop's bump waits on dynamic-assets having
published. Each run posts how many nodes are requesting exit, and what happened in each
repo, to the translations channel.

    session-ops run snode-list [--dry-run]

Environment:
    CROWDIN_DISCORD_WEBHOOK_URL   where the summary goes (not needed with --dry-run)
    PUBLISH_GIT_AUTHOR            "Name <email>" the commits are authored as
    plus what shared/github.py reads to publish (not needed with --dry-run)
"""
import argparse
import json
import os
import re
import tempfile

from session_ops.ops.runner import Outcome, call_target, step
from session_ops.platforms import publish, submodules
from session_ops.shared import discord, github, http
from session_ops.shared.env import get_env
from session_ops.shared.git import Repo

SEEDS = ("https://seed1.getsession.org/json_rpc",
         "https://seed2.getsession.org/json_rpc",
         "https://seed3.getsession.org/json_rpc")
# Each node's fields as the clients read them, in this order.
FIELDS = {"public_ip": str, "storage_port": int, "pubkey_ed25519": str,
          "pubkey_x25519": str, "requested_unlock_height": int, "storage_lmq_port": int,
          "storage_server_version": list, "swarm": str}
# The network has over a thousand; fewer means a seed answered from a broken view of it.
MIN_NODES = 20

ASSETS = "session-desktop-dynamic-assets"
ASSETS_PATH = "service-nodes-cache.json"
ASSETS_MESSAGE = "chore: update snode cache"

DESKTOP = submodules.Submodule("session-desktop", "dev", "dynamic_assets")
DESKTOP_BRANCH = "update-dynamic-assets"
DESKTOP_TITLE = "chore: Update dynamic assets submodule"
DESKTOP_BODY = """[Automated]
Moves the `session-desktop-dynamic-assets` submodule to the latest service node list.
"""

IOS = "session-ios"
IOS_PATH = "Session/Meta/service-nodes-cache.json"
IOS_BRANCH = "feature/update-static-snode-list"
IOS_TITLE = "[Automated] Update fallback static snode list"
IOS_BODY = """[Automated]
This PR updates the static service node list which is used as a fallback when a new client is unable to contact the seed nodes
"""

TARGETS = ("dynamic-assets", "desktop", "ios")


def ask(session, seed):
    resp = session.request("POST", seed, json={"method": "get_service_nodes", "params": {
        "active_only": True, "fields": {**dict.fromkeys(FIELDS, True), "height": True}}})
    if resp.status_code != 200:
        raise RuntimeError(f"answered {resp.status_code}")
    try:
        result = resp.json()["result"]
        nodes, height = result["service_node_states"], result["height"]
    except (ValueError, KeyError, TypeError) as exc:
        raise RuntimeError(f"no service_node_states in its answer ({exc!r})") from exc
    if not isinstance(nodes, list) or not isinstance(height, int):
        raise RuntimeError("no service_node_states in its answer")
    for node in nodes:
        if not isinstance(node, dict) or not all(
                isinstance(node.get(name), kind) for name, kind in FIELDS.items()):
            raise RuntimeError(f"a node lacks one of {', '.join(FIELDS)}: {node!r:.200}")
    nodes = [node for node in nodes if node["public_ip"] not in ("", "0.0.0.0")]
    if len(nodes) < MIN_NODES:
        raise RuntimeError(f"only {len(nodes)} usable nodes, fewer than {MIN_NODES}")
    return nodes, height


def render(nodes, height):
    """The list as the clients bundle it: sorted by key, so a run's diff is only what
    changed on the network."""
    nodes = sorted(nodes, key=lambda node: node["pubkey_ed25519"])
    return json.dumps({"service_node_states": [{name: node[name] for name in FIELDS}
                                               for node in nodes],
                       "height": height}, indent=2, ensure_ascii=False).encode()


def fetch(session):
    """The first seed's list that holds enough nodes, each with every field."""
    errors = []
    for seed in SEEDS:
        try:
            return render(*ask(session, seed))
        except (RuntimeError, OSError) as exc:
            errors.append(f"{seed}: {exc}")
    raise RuntimeError("no seed node gave a usable list: " + "; ".join(errors))


def write(repo, path, content):
    with open(os.path.join(repo.path, path), "wb") as handle:
        handle.write(content)


def summary(content, results):
    """The channel's line: nodes asking to exit (`requested_unlock_height` set), then
    what happened in each repo."""
    data = json.loads(content)
    nodes = data["service_node_states"]
    exiting = sum(1 for node in nodes if node.get("requested_unlock_height"))
    # <url> keeps Discord from unfurling a preview of each pull request.
    lines = [re.sub(r"(https?://\S+)", r"<\1>", result) for result in results]
    return "\n".join([f"🛰️ **snode-list**: {exiting} of {len(nodes)} service nodes are "
                      f"requesting exit at height {data.get('height')}.", *lines])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.strip().split("\n")[0])
    parser.add_argument("--dry-run", action="store_true",
                        help="Commit locally and show the change; push nothing.")
    args = parser.parse_args(argv)
    # Resolved first, so a missing secret stops the run before it publishes anything.
    webhook = get_env("CROWDIN_DISCORD_WEBHOOK_URL", required=not args.dry_run)

    author = os.environ.get("PUBLISH_GIT_AUTHOR") or "session-ops <session-ops@localhost>"
    work = os.environ.get("SESSION_OPS_WORK_DIR") or tempfile.mkdtemp(prefix="snode-list-")
    step("fetch")
    content = fetch(http.Session())
    token = None if args.dry_run else github.publish_token(publish.ORG,
                                                           [ASSETS, DESKTOP.repo, IOS])
    api = github.publish_session(token) if token else None
    results, published = [], {}

    def assets(_):
        step("dynamic-assets: publish")
        repo = Repo.sparse_clone(f"{publish.GITHUB}/{publish.ORG}/{ASSETS}", "main",
                                 os.path.join(work, ASSETS), [f"/{ASSETS_PATH}"], token)
        write(repo, ASSETS_PATH, content)
        results.append(publish.direct_push(repo, api, f"{publish.ORG}/{ASSETS}", "main",
                                           ASSETS_MESSAGE, "", author, args.dry_run))
        published["sha"] = repo.head()

    def desktop(_):
        step("desktop: publish")
        if "sha" not in published:
            raise RuntimeError(f"{ASSETS} did not publish, so there is nothing to bump to")
        results.append(submodules.bump(DESKTOP, published["sha"], work, token, api,
                                       DESKTOP_BRANCH, DESKTOP_TITLE, DESKTOP_BODY, author,
                                       args.dry_run))

    def ios(_):
        step("ios: publish")
        repo = Repo.sparse_clone(f"{publish.GITHUB}/{publish.ORG}/{IOS}", "dev",
                                 os.path.join(work, IOS), [f"/{IOS_PATH}"], token)
        write(repo, IOS_PATH, content)
        results.append(publish.pull_request(repo, api, f"{publish.ORG}/{IOS}", "dev",
                                            IOS_BRANCH, IOS_TITLE, IOS_BODY, author,
                                            args.dry_run))

    errors = {name: call_target(function, [])
              for name, function in zip(TARGETS, (assets, desktop, ios))}
    for result in results:
        print(result)
    step("report")
    message = summary(content, results + [f"{name}: failed, see the alert"
                                          for name, error in errors.items() if error])
    if args.dry_run:
        print(message)
    else:
        payload = {"content": message, "allowed_mentions": {"parse": []}}
        if discord.post_to_discord(http.Session(), webhook, [payload]) != 1:
            raise RuntimeError("Discord did not accept the summary")
    return Outcome(targets=errors)
