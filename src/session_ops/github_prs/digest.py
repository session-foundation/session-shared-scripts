#!/usr/bin/env python3
"""
Daily digest of contributor pull requests across the session-foundation org.

Lists every open PR in the org's own repositories whose author is not a maintainer,
grouped by repository, marking the ones never reported before and the ones that have
moved since they were. Posted to a Discord webhook of its own.

--state is what makes the second of those answerable: it records what reached Discord
and what each PR's updated_at was at the time, so a PR the digest already showed stays
out until something happens to it. Without it every PR in the window reads as new.

Who is a maintainer comes from maintainers.txt, one login per line; bot accounts are
dropped on GitHub's own account type rather than by name. Forks, archived and private
repos are excluded by checking the search results against the org's repository list,
so a repo created today is covered today.

One search fetches every open PR in the org, and the window is applied to the result
here rather than in the query. That is what lets the header carry the total open
contributor backlog alongside the day's changes for the cost of a single query.

Config (env vars, or flags for local runs):
    GITHUB_PRS_TOKEN      GitHub token, read-only. Needs no scope at all: the
                          digest reads public repositories only.
    GITHUB_PRS_DISCORD_WEBHOOK_URL
                          Discord incoming webhook for the channel this posts to
                          (not needed with --dry-run)
    GITHUB_PRS_ORG        (optional) org to scan; defaults to session-foundation

Usage:
    # real run (what the timer does)
    github-prs-digest

    # fetch and render, print the payload, post nothing
    github-prs-digest --dry-run

    # what the weekday timer does: a window covering the weekend, deduped
    github-prs-digest --window-hours 72 --state /var/lib/github-prs/seen.json
"""
import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from operator import itemgetter

import requests

from session_ops.shared import discord, state as dedup
from session_ops.shared.discord import MAX_MESSAGE_TEXT_CHARS, clip
from session_ops.shared.env import get_env
from session_ops.shared.retry import request_with_retry

API = "https://api.github.com"
DEFAULT_ORG = "session-foundation"
# Three days, because the timer runs on weekdays: Monday's window has to reach back
# over the weekend. Overlap between consecutive runs is what --state absorbs.
DEFAULT_WINDOW_HOURS = 72
DEFAULT_RETENTION_DAYS = 30
STATE_VERSION = 1
# The Search API caps a query at 1000 results and returns 422 for any page past it
# (at per_page=100 that is page 11). Past the cap the digest reports truncation
# rather than failing the run.
# https://docs.github.com/en/rest/search#about-search
SEARCH_RESULT_LIMIT = 1000
PER_PAGE = 100
MAINTAINERS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "maintainers.txt")


def load_maintainers(path):
    """Logins from maintainers.txt, lowercased. `#` comments and blanks ignored."""
    logins = set()
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            login = line.split("#", 1)[0].strip()
            if login:
                logins.add(login.lower())
    return frozenset(logins)


def github_session(token):
    session = requests.Session()
    session.headers.update({
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    return session


def fetch_json(session, url, **kwargs):
    resp = request_with_retry(session, "GET", url, **kwargs)
    if resp.status_code >= 400:
        sys.exit(f"GitHub {resp.status_code} on {url}: {resp.text[:300]}")
    return resp.json()


def fetch_repos(session, org):
    """Names of the org's own, live, public repositories.

    Private repositories are never reported, whatever the token can see: the digest
    posts to Discord, and nothing about them belongs there.
    """
    names, page = set(), 1
    while True:
        batch = fetch_json(session, f"{API}/orgs/{org}/repos",
                           params={"per_page": PER_PAGE, "page": page, "type": "all"})
        for repo in batch:
            if repo.get("private") or repo.get("fork") or repo.get("archived"):
                continue
            names.add(repo["name"])
        if len(batch) < PER_PAGE:
            return names
        page += 1


def search_open_prs(session, org, max_results=SEARCH_RESULT_LIMIT):
    """Every open PR in the org, newest activity first.

    Returns (items, truncated). `truncated` is the caller's cue that the counts are
    a floor rather than a total.
    """
    items, page = [], 1
    limit = min(max_results, SEARCH_RESULT_LIMIT)
    while len(items) < limit:
        payload = fetch_json(session, f"{API}/search/issues", params={
            "q": f"org:{org} is:pr is:open",
            "sort": "updated",
            "order": "desc",
            "per_page": PER_PAGE,
            "page": page,
            # The legacy issue-search syntax is gone; without this the endpoint
            # rejects the query rather than falling back to it.
            "advanced_search": "true",
        })
        batch = payload.get("items", [])
        items.extend(batch)
        total = payload.get("total_count", len(items))
        if len(batch) < PER_PAGE or len(items) >= total:
            return items[:limit], total > len(items[:limit])
        page += 1
    return items[:limit], True


def repo_name(item):
    """Repo name for a search result — the API gives only the repository's API URL."""
    return item.get("repository_url", "").rsplit("/", 1)[-1]


def is_bot(item):
    return (item.get("user") or {}).get("type") == "Bot"


def author(item):
    return (item.get("user") or {}).get("login") or "?"


def parse_time(value):
    return datetime.fromisoformat(value)


def contributor_prs(items, repos, maintainers):
    """The open PRs worth reporting: org repos, human authors, not maintainers."""
    keep = []
    for item in items:
        if repo_name(item) not in repos or is_bot(item):
            continue
        if author(item).lower() in maintainers:
            continue
        keep.append(item)
    return keep


def in_window(prs, cutoff):
    """The PRs that have moved at all since the cutoff. A PR nobody has touched in
    three days is not news, whatever its state here."""
    return [pr for pr in prs if parse_time(pr["updated_at"]) >= cutoff]


def pr_id(pr):
    return str(pr.get("id"))


def activity_key(pr):
    """The value a re-report is judged against.

    `updated_at` moves on any change at all, so an edit that touches several PRs at
    once — a label sweep, a base branch renamed — resurfaces every one of them. The
    accurate alternative is the head SHA and the comment counts, which are not in the
    search result and cost a request per PR that moved; this is the deliberate cheaper
    half of that trade.
    """
    return pr.get("updated_at")


# ---- Dedup state -----------------------------------------------------------


def empty_state():
    return dedup.empty_state(STATE_VERSION)


def load_state(path):
    return dedup.load_state(path, STATE_VERSION, "PR")


def partition_by_state(prs, state):
    """Split into (new, changed, unchanged) against what was last reported."""
    seen = state.get("seen", {})
    new, changed, unchanged = [], [], []
    for pr in prs:
        previous = seen.get(pr_id(pr))
        if previous is None:
            new.append(pr)
        elif previous.get("updated_at") != activity_key(pr):
            changed.append(pr)
        else:
            unchanged.append(pr)
    return new, changed, unchanged


def save_state(path, state, reported, retention_days=DEFAULT_RETENTION_DAYS):
    """Record `reported` as seen. Returns (kept, pruned)."""
    records = {pr_id(pr): {"updated_at": activity_key(pr),
                           # Not read back. The file is the first thing anyone opens
                           # when the digest reports the wrong thing, and an id
                           # alone identifies nothing.
                           "pr": f"{repo_name(pr)}#{pr.get('number')}"}
               for pr in reported}
    return dedup.save_state(path, state, records, retention_days, STATE_VERSION)


# ---- Discord rendering -----------------------------------------------------
#
# A header, then one block per repository whose PRs changed. Components V2 so that
# each repository is its own component: a long day splits between repositories
# rather than mid-list, unless one repository alone outgrows a message.
MAX_COMPONENTS_PER_MESSAGE = 10
TITLE_CHARS = 90

NEW_MARKER = "🟢"
UPDATED_MARKER = "✏️"


def age(then, now):
    """Compact age, coarsening as it grows: 40m, 6h, 3d, 5w."""
    minutes = max(0, int((now - then).total_seconds() // 60))
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    if hours < 48:
        return f"{hours}h"
    days = hours // 24
    if days < 14:
        return f"{days}d"
    return f"{days // 7}w"


def build_pr_line(pr, now, is_new):
    marker = NEW_MARKER if is_new else UPDATED_MARKER
    stamp = age(parse_time(pr["created_at"] if is_new else pr["updated_at"]), now)
    title = clip(pr.get("title"), TITLE_CHARS)
    if pr.get("draft"):
        title = f"[draft] {title}"
    comments = pr.get("comments") or 0
    replies = f" · 💬{comments}" if comments else ""
    return (f"{marker} [#{pr['number']}]({pr['html_url']}) @{author(pr)} · "
            f"{stamp}{replies} · {title}")


def split_block(heading, entries, max_chars):
    """(text, ids) blocks of at most `max_chars`, each under its own copy of `heading`.

    A block over the budget is rejected by Discord, and since nothing in it is then
    recorded, the same block would be rebuilt every run until the window moved on.
    """
    blocks, lines, ids, used = [], [heading], set(), len(heading)
    for line, key in entries:
        if ids and used + 1 + len(line) > max_chars:
            blocks.append(("\n".join(lines), ids))
            lines, ids, used = [heading], set(), len(heading)
        lines.append(line)
        ids.add(key)
        used += 1 + len(line)
    blocks.append(("\n".join(lines), ids))
    return blocks


def group_by_repo(new, updated, now, max_chars=MAX_MESSAGE_TEXT_CHARS):
    """(text, ids) per repository, new PRs above updated ones.

    Repositories are ordered by how much changed, so the busiest is read first.
    Within a group each PR sorts on the timestamp its line shows — the search
    returns them in update order, which reads as no order at all next to an age
    taken from the creation date.
    """
    repos = []
    for repo in sorted({repo_name(pr) for pr in new + updated}):
        entries = []
        for prs, field, is_new in ((new, "created_at", True),
                                   (updated, "updated_at", False)):
            group = sorted((pr for pr in prs if repo_name(pr) == repo),
                           key=itemgetter(field), reverse=True)
            entries += [(build_pr_line(pr, now, is_new), pr_id(pr)) for pr in group]
        repos.append((split_block(f"**{repo}**", entries, max_chars), len(entries)))
    repos.sort(key=lambda item: -item[1])
    return [block for blocks, _ in repos for block in blocks]


def window_label(hours):
    if hours % 24 == 0 and hours >= 24:
        days = hours // 24
        return f"{days} day{'s' if days > 1 else ''}"
    return f"{hours}h"


def build_header(new, updated, backlog, window_hours, truncated):
    lines = [f"**Contributor pull requests** · last {window_label(window_hours)}"]
    if new or updated:
        lines.append(f"{NEW_MARKER} **{len(new)}** new · "
                     f"{UPDATED_MARKER} **{len(updated)}** updated")
    else:
        lines.append("Nothing opened or updated.")
    lines.append(f"**{backlog}** open from contributors across the org.")
    if truncated:
        lines.append("_GitHub capped the search at 1000 results; the counts are a floor._")
    return "\n".join(lines)


def build_messages(new, updated, backlog, window_hours, now, truncated=False):
    """Return (messages, coverage), as shared.discord.messages_from_entries does."""
    header = build_header(new, updated, backlog, window_hours, truncated)
    # Every block is sized to fit beside the header, though only the first message
    # carries it: simpler than sizing the first block differently.
    blocks = group_by_repo(new, updated, now, MAX_MESSAGE_TEXT_CHARS - len(header))
    return discord.messages_from_entries(header, blocks, MAX_COMPONENTS_PER_MESSAGE)


def main():
    parser = argparse.ArgumentParser(
        description="Post a daily digest of contributor pull requests to Discord.")
    parser.add_argument("--org", help=f"GitHub org to scan (else GITHUB_PRS_ORG, default {DEFAULT_ORG}).")
    parser.add_argument("--token", help="GitHub token (else GITHUB_PRS_TOKEN).")
    parser.add_argument("--webhook", help="Discord webhook URL (else GITHUB_PRS_DISCORD_WEBHOOK_URL).")
    parser.add_argument("--maintainers", default=MAINTAINERS_FILE,
                        help="Logins to treat as maintainers, one per line.")
    parser.add_argument("--window-hours", type=int, default=DEFAULT_WINDOW_HOURS,
                        help=f"How far back a PR must have moved to be considered "
                             f"(default {DEFAULT_WINDOW_HOURS}).")
    parser.add_argument("--state", metavar="PATH",
                        help="Dedup state: without it every PR in the window is new.")
    parser.add_argument("--state-retention-days", type=int,
                        default=DEFAULT_RETENTION_DAYS, metavar="N",
                        help=f"Drop state entries older than this "
                             f"(default {DEFAULT_RETENTION_DAYS}).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print the Discord payload instead of posting it.")
    args = parser.parse_args()

    if args.window_hours < 1:
        sys.exit("--window-hours must be at least 1.")

    org = args.org or os.environ.get("GITHUB_PRS_ORG") or DEFAULT_ORG
    token = get_env("GITHUB_PRS_TOKEN", args.token)
    webhook = get_env("GITHUB_PRS_DISCORD_WEBHOOK_URL", args.webhook,
                      required=not args.dry_run)
    maintainers = load_maintainers(args.maintainers)

    session = github_session(token)
    repos = fetch_repos(session, org)
    items, truncated = search_open_prs(session, org)
    prs = contributor_prs(items, repos, maintainers)

    now = datetime.now(timezone.utc)
    state = load_state(args.state)
    new, changed, unchanged = partition_by_state(
        in_window(prs, now - timedelta(hours=args.window_hours)), state)
    print(f"{len(items)} open PRs in {org}, {len(prs)} from contributors across "
          f"{len(repos)} repos: {len(new)} new, {len(changed)} changed since last "
          f"reported, {len(unchanged)} unchanged (skipped).")

    messages, coverage = build_messages(new, changed, len(prs), args.window_hours,
                                        now, truncated)
    if args.dry_run:
        print(json.dumps(messages, indent=2, ensure_ascii=False))
        return

    posted = discord.post_to_discord(requests.Session(),
                                     discord.components_webhook_url(webhook), messages)
    # Only what Discord accepted. A PR in a message that never landed stays eligible.
    if args.state:
        landed = set().union(*coverage[:posted]) if posted else set()
        reported = [pr for pr in new + changed if pr_id(pr) in landed]
        kept, pruned = save_state(args.state, state, reported,
                                  args.state_retention_days)
        print(f"State: {len(reported)} recorded, {kept} tracked "
              f"({pruned} pruned beyond {args.state_retention_days} days).")
    if posted < len(messages):
        sys.exit(f"Posted {posted} of {len(messages)} messages.")


if __name__ == "__main__":
    main()
