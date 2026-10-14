"""What to post, given a platform's last state and this run's versions: no IO.

A platform's state is a dict: the GitHub and store versions last seen, the store's
highest ever (`store_high`), whether a warning is out (`warning`) and when it was last
posted (`warned_at`, epoch seconds).
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


def evaluate(prev, github, store, *, strict_sync=False, now=0.0, reminder_seconds=None):
    """The events for this run, and the state to keep once they are posted.

    App stores warn only when the store is ahead of GitHub: a public build whose source
    is not published. A `strict_sync` channel is ours and must match GitHub, so it
    warns in either direction. An unresolved warning repeats every `reminder_seconds`,
    and at once if the leading side pulls further ahead.
    """
    order = compare(store, github)
    store_ahead, store_behind = order > 0, order < 0
    out_of_sync = order != 0 if strict_sync else store_ahead

    # Store APIs are eventually consistent: iTunes and Flathub's CDN re-serve the
    # previous version for hours after a release. The true version is kept, but only a
    # new high is announced, so a dip and recovery stays quiet.
    prev_high = prev and (prev.get("store_high") or prev["store"])
    new_high = prev_high is None or compare(store, prev_high) > 0
    state = {"github": github, "store": store, "store_high": store if new_high else prev_high,
             "warning": out_of_sync, "warned_at": None}

    if prev is None:
        if out_of_sync:
            state["warned_at"] = now
            return Result([Event(WARNING, github, store, initial=True)], state)
        return Result([], state)

    github_advanced = compare(github, prev["github"]) > 0
    store_advanced = compare(store, prev["store"]) > 0
    warned_at = now if prev.get("warned_at") is None else prev["warned_at"]
    events = []

    if out_of_sync and not prev["warning"]:
        events.append(Event(WARNING, github, store))
        warned_at = now
    elif out_of_sync:
        if (store_ahead and store_advanced) or (store_behind and github_advanced):
            events.append(Event(WARNING_UPDATE, github, store))
            warned_at = now
        elif reminder_seconds is not None and now - warned_at >= reminder_seconds:
            events.append(Event(WARNING_REMINDER, github, store))
            warned_at = now
    elif prev["warning"]:
        events.append(Event(ALL_CLEAR, github, store))

    if not events:
        if github_advanced:
            events.append(Event(INFO_GITHUB, github, store))
        if new_high:
            events.append(Event(INFO_STORE, github, store))

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
