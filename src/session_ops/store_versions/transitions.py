"""What to post, given a platform's last state and this run's versions: no IO.

A platform's state is a dict: the GitHub and store versions last seen, the store's
recent highest (`store_high`) and since when it has served lower (`low_since`), whether
a warning is out (`warning`) and when it was last posted (`warned_at`, epoch seconds).
"""
from dataclasses import dataclass, field

from session_ops.store_versions.sources import compare

WARNING = "warning"
WARNING_UPDATE = "warning_update"
WARNING_REMINDER = "warning_reminder"
ALL_CLEAR = "all_clear"
INFO_GITHUB = "info_github"
INFO_STORE = "info_store"
WARNINGS = (WARNING, WARNING_UPDATE, WARNING_REMINDER)

# Longer than the hours iTunes and Flathub's CDN take to stop re-serving an old version.
WATERMARK_SECONDS = 6 * 3600

SOURCE_FAILING = "source_failing"
SOURCE_RECOVERED = "source_recovered"


@dataclass(frozen=True)
class Event:
    kind: str
    github: str
    store: str
    # Raised on a platform's first check, which found it already out of sync.
    initial: bool = False


@dataclass
class Result:
    events: list = field(default_factory=list)
    state: dict = field(default_factory=dict)


def evaluate(prev, github, store, *, prereleases=(), strict_sync=False, now=0.0,
             reminder_seconds=None, watermark_seconds=WATERMARK_SECONDS):
    """The events for this run, and the state to keep once they are posted.

    App stores warn only when the store is ahead of GitHub: a public build whose source
    is not published. A `strict_sync` channel is ours and must match GitHub, so it
    warns in either direction. An unresolved warning repeats every `reminder_seconds`,
    and at once if the leading side pulls further ahead. `prereleases` are the
    published prereleases above `github`: a store shipping one of them is in sync.
    """
    # Store APIs are eventually consistent: iTunes and Flathub's CDN re-serve the
    # previous version for hours after a release. Every comparison uses the highest
    # version the store has served, so a dip and recovery stays quiet, until the store
    # has served lower for `watermark_seconds`: a rollback or a bad read, not a lag.
    prev_high = prev and (prev.get("store_high") or prev["store"])
    new_high = prev_high is None or compare(store, prev_high) > 0
    high, low_since = (store if new_high else prev_high), None
    if not new_high and compare(store, prev_high) < 0:
        low_since = now if prev.get("low_since") is None else prev["low_since"]
        if now - low_since >= watermark_seconds:
            high, low_since = store, None
    if compare(high, github) > 0 and any(compare(high, p) == 0 for p in prereleases):
        github = high

    order = compare(high, github)
    store_ahead, store_behind = order > 0, order < 0
    out_of_sync = order != 0 if strict_sync else store_ahead
    state = {"github": github, "store": store, "store_high": high, "low_since": low_since,
             "warning": out_of_sync, "warned_at": None}

    if prev is None:
        if out_of_sync:
            state["warned_at"] = now
            return Result([Event(WARNING, github, high, initial=True)], state)
        return Result([], state)

    github_advanced = compare(github, prev["github"]) > 0
    warned_at = now if prev.get("warned_at") is None else prev["warned_at"]
    events = []

    if out_of_sync and not prev["warning"]:
        events.append(Event(WARNING, github, high))
        warned_at = now
    elif out_of_sync:
        if (store_ahead and new_high) or (store_behind and github_advanced):
            events.append(Event(WARNING_UPDATE, github, high))
            warned_at = now
        elif reminder_seconds is not None and now - warned_at >= reminder_seconds:
            events.append(Event(WARNING_REMINDER, github, high))
            warned_at = now
    elif prev["warning"]:
        events.append(Event(ALL_CLEAR, github, high))

    if not events:
        if github_advanced:
            events.append(Event(INFO_GITHUB, github, high))
        if new_high:
            events.append(Event(INFO_STORE, github, high))

    state["warned_at"] = warned_at if out_of_sync else None
    return Result(events, state)


def source_events(prev, error, *, now, after_seconds, reminder_seconds):
    """(event or None, state) for one source: `error` is this run's failure, or None.

    A source can fail for a run or two (Play throttles a scrape, a CDN hiccups), so it
    is reported only once it has failed for `after_seconds`, then every
    `reminder_seconds`, and its recovery only if its failure was reported.
    """
    if error is None:
        recovered = prev and prev.get("alerted_at") is not None
        return (SOURCE_RECOVERED if recovered else None), None
    since = prev["since"] if prev else now
    alerted_at = prev.get("alerted_at") if prev else None
    state = {"since": since, "alerted_at": alerted_at, "error": error}
    due = (now - since >= after_seconds if alerted_at is None
           else now - alerted_at >= reminder_seconds)
    if due:
        state["alerted_at"] = now
        return SOURCE_FAILING, state
    return None, state
