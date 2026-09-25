"""The job registry, jobs.toml: what runs, as whom, when, and what it needs.

It drives `session-ops run`, the systemd drop-ins `session-ops units` writes, and the
silence checker, so a job is added in one place.
"""
import os
import tomllib
from dataclasses import dataclass, field

REGISTRY = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "jobs.toml")
STATE_ROOT = "/var/lib/session-ops"


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

    @property
    def state_dir(self):
        return os.path.join(STATE_ROOT, self.name)

    def argv(self, dry_run=False, state_dir=None):
        """The job's arguments, with {state} standing for its state directory."""
        state = state_dir or self.state_dir
        args = [a.replace("{state}", state) for a in self.args]
        return args + list(self.dry_run_args) if dry_run else args


def load(path=REGISTRY):
    with open(path, "rb") as handle:
        rows = tomllib.load(handle).get("job", [])
    jobs, seen = [], set()
    for row in rows:
        name = row.get("name")
        missing = [k for k in ("name", "entry", "description", "user", "env_files")
                   if not row.get(k)]
        if missing:
            raise ValueError(f"{path}: job {name or '?'} lacks {', '.join(missing)}")
        if name in seen:
            raise ValueError(f"{path}: job {name} is listed twice")
        if row.get("schedule") and not isinstance(row.get("max_age_hours"), (int, float)):
            raise ValueError(f"{path}: scheduled job {name} needs a numeric max_age_hours")
        seen.add(name)
        jobs.append(Job(**{k: tuple(v) if isinstance(v, list) else v
                           for k, v in row.items()}))
    return jobs


def get(name, path=REGISTRY):
    for job in load(path):
        if job.name == name:
            return job
    raise KeyError(name)
