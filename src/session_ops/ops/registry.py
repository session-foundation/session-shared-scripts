"""The job registry, jobs.toml: what runs, as whom, when, and what it needs.

It drives `session-ops run`, the systemd drop-ins `session-ops units` writes, and the
silence checker, so a job is added in one place.
"""
import os
import tomllib
from dataclasses import dataclass, field, replace

REGISTRY = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "jobs.toml")
STATE_ROOT = "/var/lib/session-ops"
QUEUE_TIMER = "session-ops-queue.timer"


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

    @property
    def scheduled(self):
        return bool(self.schedule or self.queued)

    @property
    def timer(self):
        return QUEUE_TIMER if self.queued else f"session-ops@{self.name}.timer"

    @property
    def state_dir(self):
        return os.path.join(STATE_ROOT, self.name)

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
        if job.scheduled and not isinstance(job.max_age_hours, (int, float)):
            raise ValueError(f"{path}: scheduled job {job.name} needs a numeric max_age_hours")
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
