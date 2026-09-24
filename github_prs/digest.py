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

The helpers here deliberately duplicate their zendesk_triage counterparts rather
than importing them: the two jobs run under different users from different env
files, and a shared module would make either one's dependencies the other's.

Config (env vars, or flags for local runs):
    GITHUB_PRS_TOKEN      GitHub token, read-only. Needs no scope at all: the
                          digest reads public repositories only.
    GITHUB_PRS_DISCORD_WEBHOOK_URL
                          Discord incoming webhook for the channel this posts to
                          (not needed with --dry-run)
    GITHUB_PRS_ORG        (optional) org to scan; defaults to session-foundation

Usage:
    # real run (what the timer does)
    python digest.py

    # fetch and render, print the payload, post nothing
    python digest.py --dry-run

    # what the weekday timer does: a window covering the weekend, deduped
    python digest.py --window-hours 72 --state /var/lib/github-prs/seen.json
"""
import argparse
import json
import math
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from operator import itemgetter
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests

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


def get_env(name, cli_value=None, required=True):
    if cli_value:
        return cli_value
    value = os.environ.get(name)
    if value:
        return value
    if required:
        sys.exit(f"Missing required config: set the {name} environment variable (or pass the matching flag).")
    return None


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


def retry_after_seconds(resp, default):
    """Seconds to wait per the response, falling back to `default`.

    GitHub answers a secondary rate limit with Retry-After, and a primary one with
    x-ratelimit-reset as an epoch second and no Retry-After at all, so both are
    read. Anything unparseable, negative or infinite falls back rather than taking
    the run down inside time.sleep().
    """
    raw = resp.headers.get("retry-after")
    if raw is None:
        reset = resp.headers.get("x-ratelimit-reset")
        if reset is None:
            return default
        try:
            return max(0.0, float(reset) - time.time())
        except (TypeError, ValueError):
            return default
    try:
        seconds = float(raw)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(seconds) or seconds < 0:
        return default
    return seconds


def request_with_retry(session, method, url, attempts=5, **kwargs):
    """GET/POST with backoff on 429 and 5xx."""
    if attempts <= 0:
        raise ValueError("attempts must be at least 1")

    delay = 1.0
    last_exc = None
    resp = None
    for attempt in range(attempts):
        final = attempt == attempts - 1
        try:
            resp = session.request(method, url, timeout=30, **kwargs)
        except requests.RequestException as exc:
            last_exc = exc
            if final:
                break
            time.sleep(delay)
            delay = min(delay * 2, 30)
            continue
        if resp.status_code == 429 or resp.status_code >= 500:
            if final:
                break
            time.sleep(min(retry_after_seconds(resp, delay), 60))
            delay = min(delay * 2, 30)
            continue
        return resp
    if last_exc:
        raise last_exc
    return resp


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
#
# Which PRs have already been reported, and what their activity looked like then.
# Every failure to read it is a cache miss rather than an error: losing the file
# re-reports the window once, which is noisy and never wrong, and that is what makes
# it a cache rather than something to back up.


def empty_state():
    return {"version": STATE_VERSION, "seen": {}}


def load_state(path):
    if not path:
        return empty_state()
    if not os.path.exists(path):
        print(f"No state file at {path}; treating every PR in the window as new.")
        return empty_state()
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Note: unreadable state file {path} ({exc}); treating every PR as new.")
        return empty_state()
    if not isinstance(data, dict) or not isinstance(data.get("seen"), dict):
        print(f"Note: unexpected shape in {path}; treating every PR as new.")
        return empty_state()
    # A file written by another schema version cannot be trusted field by field, so
    # it is a cache miss rather than something to misread.
    if data.get("version") != STATE_VERSION:
        print(f"Note: {path} is version {data.get('version')!r}, expected "
              f"{STATE_VERSION}; treating every PR as new.")
        return empty_state()
    print(f"Loaded state for {len(data['seen'])} previously reported PRs.")
    return data


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
    """Record `reported` as seen, prune old entries, write atomically.

    Returns (kept, pruned).
    """
    now = datetime.now(timezone.utc)
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    seen = dict(state.get("seen", {}))
    for pr in reported:
        seen[pr_id(pr)] = {
            "updated_at": activity_key(pr),
            "last_reported": stamp,
            # Not read back. The file is the first thing anyone opens when the digest
            # reports the wrong thing, and an id alone identifies nothing.
            "pr": f"{repo_name(pr)}#{pr.get('number')}",
        }

    cutoff = now - timedelta(days=retention_days)
    kept = {}
    for key, record in seen.items():
        try:
            last = datetime.strptime(record.get("last_reported", ""),
                                     "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            continue  # malformed entry — drop it rather than keep it forever
        if last >= cutoff:
            kept[key] = record

    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    temporary = f"{path}.tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump({"version": STATE_VERSION, "updated_at": stamp, "seen": kept},
                  handle, indent=2)
    os.replace(temporary, path)  # atomic: a crash mid-write cannot corrupt the state
    return len(kept), len(seen) - len(kept)


# ---- Discord rendering -----------------------------------------------------
#
# A header, then one block per repository whose PRs changed. Components V2 so that
# each repository is its own component: a long day splits between repositories
# rather than mid-list, unless one repository alone outgrows a message.
COMPONENTS_V2_FLAG = 1 << 15
CONTAINER = 17
TEXT_DISPLAY = 10
SEPARATOR = 14
# Discord's ceiling on all the text in one Components V2 message.
MAX_MESSAGE_TEXT_CHARS = 4000
MAX_COMPONENTS_PER_MESSAGE = 10
TITLE_CHARS = 90

NEW_MARKER = "🟢"
UPDATED_MARKER = "✏️"


def clip(text, limit):
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


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


def chunk_blocks(blocks, first_used=0,
                 max_items=MAX_COMPONENTS_PER_MESSAGE,
                 max_chars=MAX_MESSAGE_TEXT_CHARS):
    """Group rendered blocks into messages within Discord's budgets.

    `first_used` is what the header has already spent on the first message; without
    it a busy day's first message goes over on the header alone.

    A single block over the character budget still gets its own message rather than
    being dropped; group_by_repo splits blocks to keep that from arising.
    """
    chunks, current, used = [], [], first_used
    for block in blocks:
        cost = len(block[0]) if isinstance(block, tuple) else len(block)
        if current and (len(current) >= max_items or used + cost > max_chars):
            chunks.append(current)
            current, used = [], 0
        current.append(block)
        used += cost
    if current:
        chunks.append(current)
    return chunks


def build_messages(new, updated, backlog, window_hours, now, truncated=False):
    """Return (messages, coverage).

    coverage[i] is the set of PR ids message i accounts for, so a run that fails
    partway through can still record exactly what reached Discord — otherwise a
    failure on the last message re-posts the first ones tomorrow.
    """
    header = build_header(new, updated, backlog, window_hours, truncated)
    # Every block is sized to fit beside the header, though only the first message
    # carries it: simpler than sizing the first block differently.
    blocks = group_by_repo(new, updated, now, MAX_MESSAGE_TEXT_CHARS - len(header))
    messages, coverage = [], []
    # A quiet day still owes the channel its header, so seed one empty chunk.
    for index, chunk in enumerate(chunk_blocks(blocks, first_used=len(header)) or [[]]):
        components = []
        if index == 0:
            components.append({"type": TEXT_DISPLAY, "content": header})
            if chunk:
                components.append({"type": SEPARATOR})
        components += [{"type": TEXT_DISPLAY, "content": text} for text, _ in chunk]
        messages.append({
            "flags": COMPONENTS_V2_FLAG,
            "components": [{"type": CONTAINER, "components": components}],
        })
        coverage.append(set().union(*(ids for _, ids in chunk)) if chunk else set())
    return messages, coverage


def components_webhook_url(webhook_url):
    """The webhook, told to respect the components field, which it ignores without."""
    parts = urlsplit(webhook_url)
    query = dict(parse_qsl(parts.query))
    query["with_components"] = "true"
    return urlunsplit(parts._replace(query=urlencode(query)))


def post_to_discord(session, url, messages):
    """POST each message in order; return how many Discord accepted."""
    for index, payload in enumerate(messages):
        try:
            resp = request_with_retry(session, "POST", url, json=payload)
        except requests.RequestException as exc:
            print(f"Discord unreachable on message {index + 1}/{len(messages)} ({exc}).")
            return index
        if resp.status_code >= 400:
            print(f"Discord rejected message {index + 1}/{len(messages)} "
                  f"({resp.status_code}): {resp.text[:300]}")
            return index
    return len(messages)


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

    posted = post_to_discord(requests.Session(),
                             components_webhook_url(webhook), messages)
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
