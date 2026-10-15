#!/usr/bin/env python3
"""
Post when a store's Session release and the GitHub release diverge.

Each run compares every platform's store version with its GitHub release and posts:

- ⚠️ a warning, tagging the platform's people, when the store is ahead of GitHub (a
  public build whose source is not out) or, for a channel we publish ourselves (APT,
  and the merged F-Droid and Flathub manifests), when it differs either way: their
  release pull requests have to be merged after each GitHub release;
- the warning again every 12 hours while unresolved, and at once if the gap grows;
- ✅ once back in sync, after a warning;
- ℹ️ when GitHub or the store moves on without a warning.

A platform seen for the first time is recorded quietly, unless already out of sync.
A source failing for an hour is posted, without tags, then every 12 hours until it
recovers.

Config:
    STORE_VERSIONS_DISCORD_WEBHOOK_URL   where it posts (not needed with --dry-run)
    /etc/session-ops/store-versions.toml who each platform's warnings tag:
        [tag]
        android = [111111111111111111]

Usage:
    store-versions --state /var/lib/session-ops/store-versions/state.json
    store-versions --dry-run       # print every version and what it would post
"""
import argparse
import json
import sys
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from session_ops.shared import discord, http
from session_ops.shared.env import get_env
from session_ops.shared.state import write_json
from session_ops.store_versions import sources
from session_ops.store_versions.transitions import (
    ALL_CLEAR, INFO_GITHUB, INFO_STORE, SOURCE_RECOVERED, WARNING,
    WARNING_REMINDER, WARNING_UPDATE, WARNINGS, evaluate, source_events)

TAGS_FILE = "/etc/session-ops/store-versions.toml"
STATE_VERSION = 1
HOUR = 3600
REMINDER_SECONDS = 12 * HOUR
FAILING_AFTER_SECONDS = HOUR
USER_AGENT = "session-ops store-versions (+https://github.com/session-foundation)"
# Short, so a host that stops answering costs under a minute of the unit's 4.
HTTP_ATTEMPTS, HTTP_TIMEOUT = 2, (5, 20)
# Messages still unposted by then wait for the next run rather than for systemd's kill.
POST_DEADLINE_SECONDS = 150
EMBED_FIELD_CHARS = 1024

ANDROID_REPO, IOS_REPO, DESKTOP_REPO = "session-android", "session-ios", "session-desktop"
ANDROID_ID = "network.loki.messenger"
FLATHUB_ID = "network.loki.Session"


@dataclass(frozen=True)
class Platform:
    name: str
    display: str
    repo: str
    store: dict
    strict_sync: bool = False


PLATFORMS = (
    Platform("android", "Session Android", ANDROID_REPO,
             {"kind": "play", "app_id": ANDROID_ID}),
    # Each Android release opens a "Release X.Y.Z" pull request on session-fdroid, which
    # has to be merged; the merged index is what this checks.
    Platform("android-fdroid-merged", "Session Android (F-Droid, merged)", ANDROID_REPO,
             {"kind": "fdroid", "app_id": ANDROID_ID,
              "base_url": "https://raw.githubusercontent.com/session-foundation/"
                          "session-fdroid/master/fdroid/repo"},
             strict_sync=True),
    # What F-Droid clients pull: behind is deploy lag, so app-store rules.
    Platform("android-fdroid-live", "Session Android (F-Droid, live)", ANDROID_REPO,
             {"kind": "fdroid", "app_id": ANDROID_ID,
              "base_url": "https://fdroid.getsession.org/fdroid/repo"}),
    Platform("ios", "Session iOS", IOS_REPO,
             {"kind": "appstore", "bundle_id": "com.loki-project.loki-messenger"}),
    # session-desktop, not session-messenger-desktop: that one is an old transitional package.
    Platform("desktop", "Session Desktop", DESKTOP_REPO,
             {"kind": "deb", "base_url": "https://deb.session.foundation",
              "package": "session-desktop"},
             strict_sync=True),
    # flathubbot opens an update pull request on flathub/network.loki.Session per
    # release, which has to be merged.
    Platform("desktop-flathub-merged", "Session Desktop (Flathub, merged)", DESKTOP_REPO,
             {"kind": "flathub_manifest", "app_id": FLATHUB_ID}, strict_sync=True),
    # Behind is Flathub's build and publish lag after the merge.
    Platform("desktop-flathub-live", "Session Desktop (Flathub, live)", DESKTOP_REPO,
             {"kind": "flathub", "app_id": FLATHUB_ID}),
)

STORE_NAMES = {"play": "Play Store", "appstore": "App Store", "deb": "APT repo",
               "fdroid": "F-Droid repo", "flathub": "Flathub",
               "flathub_manifest": "Flathub manifest"}

RED, GREEN, BLUE = 0xE74C3C, 0x2ECC71, 0x3498DB


def store_name(platform):
    return STORE_NAMES[platform.store["kind"]]


def load_tags(path):
    """{platform: [user ids]}; nobody is tagged without the file."""
    try:
        with open(path, "rb") as handle:
            data = tomllib.load(handle)
    except FileNotFoundError:
        return {}
    except OSError as exc:
        sys.exit(f"{path}: {exc.strerror}; it has to be readable by the job (mode 644)")
    except tomllib.TOMLDecodeError as exc:
        sys.exit(f"{path}: {exc}")
    tags = data.get("tag", {})
    known = {platform.name for platform in PLATFORMS}
    for name, ids in tags.items():
        if name not in known:
            sys.exit(f"{path}: [tag] names {name}, which is not one of {', '.join(sorted(known))}")
        if not (isinstance(ids, list) and all(isinstance(i, int) for i in ids)):
            sys.exit(f"{path}: [tag] {name} must be a list of Discord user ids, as integers")
    return {name: [str(i) for i in ids] for name, ids in tags.items()}


def load_state(path):
    """Unreadable or another version is an empty state: every platform is then seen for
    the first time, quietly unless out of sync."""
    data = {}
    if path:
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            pass
    if not isinstance(data, dict) or data.get("version") != STATE_VERSION:
        data = {}
    return {"version": STATE_VERSION, "platforms": data.get("platforms", {}),
            "sources": data.get("sources", {})}


def gap(platform, event):
    store = store_name(platform)
    if sources.compare(event.store, event.github) > 0:
        return (f"{store} is ahead of GitHub",
                f"{store} has published **{event.store}**, but the latest GitHub release "
                f"is **{event.github}**.")
    return (f"{store} is behind GitHub",
            f"GitHub has published **{event.github}**, but {store} is still at "
            f"**{event.store}**.")


def event_message(platform, event, github_url, store_url, tags):
    store = store_name(platform)
    fields = [{"name": f"{store} version", "value": event.store, "inline": True},
              {"name": "GitHub version", "value": event.github, "inline": True}]
    fields += [{"name": name, "value": url} for name, url in
               ((f"{store} link", store_url), ("GitHub release", github_url))
               if url and len(url) <= EMBED_FIELD_CHARS]
    embed = {"fields": fields}
    message = {"embeds": [embed], "allowed_mentions": {"parse": []}}

    if event.kind in WARNINGS:
        direction, description = gap(platform, event)
        suffix = {WARNING: " (first check)" if event.initial else "",
                  WARNING_UPDATE: " — divergence grew",
                  WARNING_REMINDER: " — still unresolved"}[event.kind]
        if event.kind == WARNING_REMINDER:
            description += f"\nStill out of sync after ~{REMINDER_SECONDS // HOUR}h."
        embed.update(color=RED, title=f"⚠️ {platform.display}: {direction}{suffix}",
                     description=description)
        ids = tags.get(platform.name, [])
        if ids:
            message["content"] = " ".join(f"<@{i}>" for i in ids)
            message["allowed_mentions"] = {"users": ids}
    elif event.kind == ALL_CLEAR:
        embed.update(color=GREEN, title=f"✅ {platform.display}: back in sync",
                     description=f"{store} and GitHub are back in sync at **{event.github}**.")
    elif event.kind == INFO_GITHUB:
        embed.update(color=BLUE, title=f"ℹ️ {platform.display}: new GitHub release",
                     description=f"GitHub advanced to **{event.github}** "
                                 f"({store} is at **{event.store}**).")
    elif event.kind == INFO_STORE:
        embed.update(color=BLUE, title=f"ℹ️ {platform.display}: {store} updated",
                     description=f"{store} advanced to **{event.store}** "
                                 f"(GitHub is at **{event.github}**).")
    return message


def combined(messages):
    """A platform's events as one message, so they land or fail together."""
    if len(messages) < 2:
        return messages
    merged = {"embeds": [embed for message in messages for embed in message["embeds"]],
              "allowed_mentions": {"parse": []}}
    for message in messages:
        if "content" in message:
            merged.update(content=message["content"],
                          allowed_mentions=message["allowed_mentions"])
    return [merged]


def source_message(source, kind, failure, now):
    if kind == SOURCE_RECOVERED:
        embed = {"color": GREEN, "title": f"✅ {source}: readable again"}
    else:
        hours = max(1, round((now - failure["since"]) / HOUR))
        embed = {"color": RED, "title": f"⚠️ {source}: failing for ~{hours}h",
                 "description": discord.clip(failure["error"], 1000)}
    return {"embeds": [embed], "allowed_mentions": {"parse": []}}


def http_session():
    session = http.Session(attempts=HTTP_ATTEMPTS, timeout=HTTP_TIMEOUT)
    session.headers["User-Agent"] = USER_AGENT
    return session


def fetch_all(wanted):
    """{key: (Release, None) or (None, error)} for {key: (fetch, args)}, all at once and
    each on its own session, so one host that stops answering holds up no other."""
    def one(fetch, args):
        try:
            return fetch(http_session(), *args), None
        except Exception as exc:  # one source down must not stop the others
            return None, f"{type(exc).__name__}: {exc}"

    with ThreadPoolExecutor(max_workers=max(1, len(wanted))) as pool:
        futures = {key: pool.submit(one, fetch, args) for key, (fetch, args) in wanted.items()}
    results = {key: future.result() for key, future in futures.items()}
    for key, (_, error) in results.items():
        if error:
            print(f"{key}: {error}", file=sys.stderr)
    return results


def check(state, tags, now):
    """[(messages, apply)]: `apply` records a step's state once its messages are posted,
    so a failed post is retried on the next run rather than lost.

    Each source is fetched once, keyed by name: three platforms share each of
    session-android's and session-desktop's releases, and unauthenticated GitHub allows
    60 requests an hour."""
    keys = {platform.name: (f"GitHub {platform.repo}",
                            f"{store_name(platform)} ({platform.name})")
            for platform in PLATFORMS}
    wanted = {}
    for platform in PLATFORMS:
        github_key, store_key = keys[platform.name]
        wanted[github_key] = (sources.fetch_github, (platform.repo,))
        wanted[store_key] = (sources.fetch_store, (platform.store,))
    results = fetch_all(wanted)

    steps = []
    for platform in PLATFORMS:
        github_key, store_key = keys[platform.name]
        (github, github_error), (store, store_error) = results[github_key], results[store_key]
        if github_error or store_error:
            print(f"{platform.name}: skipped")
            continue
        print(f"{platform.name}: github={github.version} {platform.store['kind']}={store.version}")
        result = evaluate(state["platforms"].get(platform.name), github.version, store.version,
                          strict_sync=platform.strict_sync, now=now,
                          reminder_seconds=REMINDER_SECONDS)
        messages = combined([event_message(platform, event, github.url, store.url, tags)
                             for event in result.events])
        steps.append((messages, lambda name=platform.name, new=result.state:
                      state["platforms"].__setitem__(name, new)))

    for source in sorted(set(results) | set(state["sources"])):
        error = results.get(source, (None, None))[1]
        kind, failure = source_events(state["sources"].get(source), error, now=now,
                                      after_seconds=FAILING_AFTER_SECONDS,
                                      reminder_seconds=REMINDER_SECONDS)
        messages = [source_message(source, kind, failure, now)] if kind else []
        steps.append((messages, lambda source=source, failure=failure:
                      state["sources"].__setitem__(source, failure) if failure
                      else state["sources"].pop(source, None)))
    return steps


def main(argv=None):
    parser = argparse.ArgumentParser(description="Post when store and GitHub releases diverge.")
    parser.add_argument("--state", metavar="PATH",
                        help="What was last seen and posted; without it every run is a first.")
    parser.add_argument("--tags", default=TAGS_FILE, metavar="PATH")
    parser.add_argument("--webhook", help="Discord webhook URL "
                                          "(else STORE_VERSIONS_DISCORD_WEBHOOK_URL).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what it would post instead; write no state.")
    args = parser.parse_args(argv)

    webhook = get_env("STORE_VERSIONS_DISCORD_WEBHOOK_URL", args.webhook,
                      required=not args.dry_run)
    tags = load_tags(args.tags)
    state = load_state(args.state)
    started = time.monotonic()
    steps = check(state, tags, time.time())
    messages = [message for step_messages, _ in steps for message in step_messages]

    if args.dry_run:
        for message in messages:
            print(json.dumps(message, ensure_ascii=False))
        print(f"Would post {len(messages)} message(s).")
        return

    session = http.Session(attempts=HTTP_ATTEMPTS, timeout=HTTP_TIMEOUT)
    posted, refused, deferred = 0, False, False
    for step_messages, apply in steps:
        if step_messages:
            if time.monotonic() - started > POST_DEADLINE_SECONDS:
                deferred = True
                continue
            landed = discord.post_to_discord(session, webhook, step_messages)
            posted += landed
            if landed < len(step_messages):
                refused = True
                continue
        apply()
        # Saved per step: a run killed mid-way reposts at most the step it was on.
        if args.state and step_messages:
            write_json(args.state, state)
    if args.state:
        write_json(args.state, state)
    if refused:
        sys.exit(f"Posted {posted} of {len(messages)} messages to Discord.")
    if deferred:
        print(f"Posted {posted} of {len(messages)} message(s); the rest go next run.")
    else:
        print(f"Posted {len(messages)} message(s).")


if __name__ == "__main__":
    main()
