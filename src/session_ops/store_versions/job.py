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
REMINDER_SECONDS = 12 * 3600
FAILING_AFTER_SECONDS = 3600
USER_AGENT = "session-ops store-versions (+https://github.com/session-foundation)"

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
    Platform("android", "Session Android", "session-android",
             {"kind": "play", "app_id": ANDROID_ID}),
    # Each Android release opens a "Release X.Y.Z" pull request on session-fdroid, which
    # has to be merged; the merged index is what this checks.
    Platform("android-fdroid-merged", "Session Android (F-Droid, merged)", "session-android",
             {"kind": "fdroid", "app_id": ANDROID_ID,
              "base_url": "https://raw.githubusercontent.com/session-foundation/"
                          "session-fdroid/master/fdroid/repo"},
             strict_sync=True),
    # What F-Droid clients pull: behind is deploy lag, so app-store rules.
    Platform("android-fdroid-live", "Session Android (F-Droid, live)", "session-android",
             {"kind": "fdroid", "app_id": ANDROID_ID,
              "base_url": "https://fdroid.getsession.org/fdroid/repo"}),
    Platform("ios", "Session iOS", "session-ios",
             {"kind": "appstore", "bundle_id": "com.loki-project.loki-messenger"}),
    # session-desktop, not session-messenger-desktop: that one is an old transitional package.
    Platform("desktop", "Session Desktop", "session-desktop",
             {"kind": "deb", "base_url": "https://deb.session.foundation",
              "package": "session-desktop"},
             strict_sync=True),
    # flathubbot opens an update pull request on flathub/network.loki.Session per
    # release, which has to be merged.
    Platform("desktop-flathub-merged", "Session Desktop (Flathub, merged)", "session-desktop",
             {"kind": "flathub_manifest", "app_id": FLATHUB_ID}, strict_sync=True),
    # Behind is Flathub's build and publish lag after the merge.
    Platform("desktop-flathub-live", "Session Desktop (Flathub, live)", "session-desktop",
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
               ((f"{store} link", store_url), ("GitHub release", github_url)) if url]
    embed = {"fields": fields}
    message = {"embeds": [embed], "allowed_mentions": {"parse": []}}

    if event.kind in WARNINGS:
        direction, description = gap(platform, event)
        suffix = {WARNING: " (first check)" if event.initial else "",
                  WARNING_UPDATE: " — divergence grew",
                  WARNING_REMINDER: " — still unresolved"}[event.kind]
        if event.kind == WARNING_REMINDER:
            description += f"\nStill out of sync after ~{REMINDER_SECONDS // 3600}h."
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


def source_message(source, kind, failure, now):
    if kind == SOURCE_RECOVERED:
        embed = {"color": GREEN, "title": f"✅ {source}: readable again"}
    else:
        hours = max(1, round((now - failure["since"]) / 3600))
        embed = {"color": RED, "title": f"⚠️ {source}: failing for ~{hours}h",
                 "description": discord.clip(failure["error"], 1000)}
    return {"embeds": [embed], "allowed_mentions": {"parse": []}}


class Fetcher:
    """Each source's Release or error, fetched once per run: three platforms share
    session-android's release, and unauthenticated GitHub allows 60 requests an hour."""

    def __init__(self, session):
        self.session = session
        self.results = {}

    def get(self, key, fetch, *args):
        if key not in self.results:
            try:
                self.results[key] = (fetch(self.session, *args), None)
            except Exception as exc:  # one source down must not stop the others
                self.results[key] = (None, f"{type(exc).__name__}: {exc}")
                print(f"{key}: {self.results[key][1]}", file=sys.stderr)
        return self.results[key]


def check(state, tags, session, now):
    """[(messages, apply)]: `apply` records a step's state once its messages are posted,
    so a failed post is retried on the next run rather than lost."""
    fetcher = Fetcher(session)
    steps = []
    for platform in PLATFORMS:
        github, github_error = fetcher.get(f"GitHub {platform.repo}", sources.fetch_github,
                                           platform.repo)
        store, store_error = fetcher.get(f"{store_name(platform)} ({platform.name})",
                                         sources.fetch_store, platform.store)
        if github_error or store_error:
            print(f"{platform.name}: skipped")
            continue
        print(f"{platform.name}: github={github.version} {platform.store['kind']}={store.version}")
        result = evaluate(state["platforms"].get(platform.name), github.version, store.version,
                          strict_sync=platform.strict_sync, now=now,
                          reminder_seconds=REMINDER_SECONDS)
        messages = [event_message(platform, event, github.url, store.url, tags)
                    for event in result.events]
        steps.append((messages, lambda name=platform.name, new=result.state:
                      state["platforms"].__setitem__(name, new)))

    for source in sorted(set(fetcher.results) | set(state["sources"])):
        error = fetcher.results.get(source, (None, None))[1]
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
    session = http.Session(attempts=3)
    session.headers["User-Agent"] = USER_AGENT
    steps = check(state, tags, session, time.time())
    messages = [message for step_messages, _ in steps for message in step_messages]

    if args.dry_run:
        for message in messages:
            print(json.dumps(message, ensure_ascii=False))
        print(f"Would post {len(messages)} message(s).")
        return

    posted = discord.post_to_discord(http.Session(), webhook, messages) if messages else 0
    reached = 0
    for step_messages, apply in steps:
        reached += len(step_messages)
        if reached <= posted:
            apply()
    if args.state:
        write_json(args.state, state)
    if posted < len(messages):
        sys.exit(f"Posted {posted} of {len(messages)} messages to Discord.")
    print(f"Posted {len(messages)} message(s).")


if __name__ == "__main__":
    main()
