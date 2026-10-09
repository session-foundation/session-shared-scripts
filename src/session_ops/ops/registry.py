"""The job registry, jobs.toml: what runs, as whom, when, and what it needs.

It drives `session-ops run`, the systemd drop-ins `session-ops units` writes, and the
silence checker, so a job is added in one place.
"""
import os
import re
import tomllib
from dataclasses import dataclass, field, replace

REGISTRY = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "jobs.toml")
STATE_ROOT = "/var/lib/session-ops"
QUEUE_TIMER = "session-ops-queue.timer"
# Discord's cap on a string option's choices, which `/run` offers these jobs as.
MAX_DISCORD_JOBS = 25
SPAN_UNITS = {
    **dict.fromkeys(("us", "usec"), 1e-6), **dict.fromkeys(("ms", "msec"), 1e-3),
    **dict.fromkeys(("", "s", "sec", "second", "seconds"), 1),
    **dict.fromkeys(("m", "min", "minute", "minutes"), 60),
    **dict.fromkeys(("h", "hr", "hour", "hours"), 3600),
    **dict.fromkeys(("d", "day", "days"), 86400),
    **dict.fromkeys(("w", "week", "weeks"), 604800),
}
SPAN_PART = re.compile(r"\s*(\d+(?:\.\d+)?)\s*([a-z]*)")


def span_seconds(text):
    """Seconds in a systemd time span such as "1h 30min"; a bare number is seconds."""
    parts = list(SPAN_PART.finditer(text))
    if not parts or "".join(part.group(0) for part in parts).strip() != text.strip():
        raise ValueError(f"{text!r} is not a time span")
    total = 0
    for part in parts:
        if part.group(2) not in SPAN_UNITS:
            raise ValueError(f"{text!r} has an unknown unit {part.group(2)!r}")
        total += float(part.group(1)) * SPAN_UNITS[part.group(2)]
    return total


@dataclass(frozen=True)
class Job:
    name: str
    entry: str
    description: str
    user: str
    env_files: tuple
    env: tuple = ()
    args: tuple = ()
    dry_run_args: tuple = ("--dry-run",)
    schedule: str = None
    max_age_hours: float = None
    channel_env: str = "ALERT_DISCORD_WEBHOOK_URL"
    timeout: str = "30min"
    unit: tuple = field(default=())
    queued: bool = False
    after: tuple = ()
    watch: str = None
    discord: bool = False

    @property
    def scheduled(self):
        return bool(self.schedule or self.queued)

    @property
    def timer(self):
        return QUEUE_TIMER if self.queued else f"session-ops@{self.name}.timer"

    @property
    def timeout_seconds(self):
        return span_seconds(self.timeout)

    @property
    def state_dir(self):
        return os.path.join(STATE_ROOT, self.name)

    @property
    def watch_glob(self):
        return os.path.join(self.state_dir, self.watch) if self.watch else None

    def argv(self, dry_run=False, state_dir=None):
        """The job's arguments, with {state} standing for its state directory."""
        state = state_dir or self.state_dir
        args = [a.replace("{state}", state) for a in self.args]
        return args + list(self.dry_run_args) if dry_run else args


@dataclass(frozen=True)
class Queue:
    """Jobs one timer starts together, each running once the one before it has ended."""
    schedule: str = None
    jobs: tuple = ()


def _read(path):
    with open(path, "rb") as handle:
        return tomllib.load(handle)


def load_queue(path=REGISTRY):
    row = _read(path).get("queue", {})
    return Queue(row.get("schedule"), tuple(row.get("jobs", ())))


def load(path=REGISTRY):
    data = _read(path)
    rows = data.get("job", [])
    jobs, seen = [], set()
    for row in rows:
        name = row.get("name")
        missing = [k for k in ("name", "entry", "description", "user", "env_files")
                   if not row.get(k)]
        if missing:
            raise ValueError(f"{path}: job {name or '?'} lacks {', '.join(missing)}")
        if name in seen:
            raise ValueError(f"{path}: job {name} is listed twice")
        seen.add(name)
        jobs.append(Job(**{k: tuple(v) if isinstance(v, list) else v
                           for k, v in row.items()}))
    jobs = _apply_queue(path, jobs, data.get("queue"))
    for job in jobs:
        if job.watch and (os.path.isabs(job.watch) or ".." in job.watch.split("/")):
            raise ValueError(f"{path}: {job.name}'s watch must stay inside its state directory")
        if job.scheduled and not isinstance(job.max_age_hours, (int, float)):
            raise ValueError(f"{path}: scheduled job {job.name} needs a numeric max_age_hours")
        if not isinstance(job.discord, bool):
            raise ValueError(f"{path}: {job.name}'s discord must be true or false")
        try:
            job.timeout_seconds
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{path}: {job.name}'s timeout: {exc}") from None
    if sum(job.discord for job in jobs) > MAX_DISCORD_JOBS:
        raise ValueError(f"{path}: Discord offers at most {MAX_DISCORD_JOBS} jobs to /run")
    return jobs


def _apply_queue(path, jobs, row):
    if row is None:
        return jobs
    order = row.get("jobs") or []
    if not row.get("schedule") or not order:
        raise ValueError(f"{path}: [queue] needs a schedule and jobs")
    by_name = {job.name: job for job in jobs}
    for name in order:
        if name not in by_name:
            raise ValueError(f"{path}: [queue] lists {name}, which is not a job")
        if by_name[name].schedule:
            raise ValueError(f"{path}: {name} is queued, so it takes no schedule of its own")
    if len(set(order)) != len(order):
        raise ValueError(f"{path}: [queue] lists a job twice")
    # Every job ahead, not just the previous one: install.sh links only the ready ones,
    # and After= orders nothing against a unit that is not being started.
    ahead = {name: tuple(order[:i]) for i, name in enumerate(order)}
    return [replace(job, queued=True, after=ahead[job.name]) if job.name in order else job
            for job in jobs]


def get(name, path=REGISTRY):
    for job in load(path):
        if job.name == name:
            return job
    raise KeyError(name)
