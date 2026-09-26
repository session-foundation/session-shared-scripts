"""The dedup state a digest keeps between runs: what it reported, and when.

Every failure to read it is a cache miss rather than an error. Losing the file
re-reports one window, which is noisy and never wrong, and that is what makes it
a cache rather than something to back up.
"""
import json
import os
from datetime import datetime, timedelta, timezone

STAMP = "%Y-%m-%dT%H:%M:%SZ"


def empty_state(version):
    return {"version": version, "seen": {}}


def load_state(path, version, noun):
    """The state at `path`, or an empty one for any file that cannot be trusted.

    `noun` names what the caller dedups, for the log line.
    """
    if not path:
        return empty_state(version)
    if not os.path.exists(path):
        print(f"No state file at {path}; treating every {noun} in the window as new.")
        return empty_state(version)
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as exc:  # ValueError covers bad JSON and bad UTF-8
        print(f"Note: unreadable state file {path} ({exc}); treating every {noun} as new.")
        return empty_state(version)
    if not isinstance(data, dict) or not isinstance(data.get("seen"), dict):
        print(f"Note: unexpected shape in {path}; treating every {noun} as new.")
        return empty_state(version)
    # A file written by another schema version cannot be trusted field by field.
    if data.get("version") != version:
        print(f"Note: {path} is version {data.get('version')!r}, expected {version}; "
              f"treating every {noun} as new.")
        return empty_state(version)
    print(f"Loaded state for {len(data['seen'])} previously reported {noun}s.")
    return data


def save_state(path, state, records, retention_days, version):
    """Merge `records` ({key: fields}) into the state as reported now, prune entries
    older than `retention_days`, write atomically. Returns (kept, pruned)."""
    now = datetime.now(timezone.utc)
    stamp = now.strftime(STAMP)
    seen = dict(state.get("seen", {}))
    for key, fields in records.items():
        seen[key] = {**fields, "last_reported": stamp}

    cutoff = now - timedelta(days=retention_days)
    kept = {}
    for key, record in seen.items():
        if not isinstance(record, dict):
            continue
        try:
            last = datetime.strptime(record.get("last_reported", ""), STAMP) \
                .replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            continue  # malformed entry: drop it rather than keep it forever
        if last >= cutoff:
            kept[key] = record

    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    temporary = f"{path}.tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump({"version": version, "updated_at": stamp, "seen": kept}, handle, indent=2)
    os.replace(temporary, path)  # atomic: a crash mid-write cannot corrupt the state
    return len(kept), len(seen) - len(kept)
