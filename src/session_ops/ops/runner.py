"""`session-ops`: run a registered job the way its timer does, list them, write units.

    session-ops list
    session-ops run github-prs-digest [--dry-run] [-- further job arguments]
    session-ops units --out /etc/systemd/system
    session-ops discord-register

A run owns what every job would otherwise repeat: checking the environment it needs,
a scratch directory, and telling Discord when it fails. The alert names the job, the
host, the step it was on, one sentence of error with secrets scrubbed, each target's
result for a job with several, and the commands to read the journal and re-run it.
The traceback goes to the journal only.

Having alerted, a run records its systemd invocation id, so the OnFailure= backstop
(session-ops-alert) knows this failure was already reported and stays quiet. The
backstop is for the failures a run cannot report itself: killed, timed out, or
Discord unreachable.

A job with `repeat_alert_hours` reports the same failure again only that often,
until a run succeeds.
"""
import argparse
import importlib
import json
import os
import re
import shutil
import socket
import sys
import tempfile
import time
import traceback
from dataclasses import dataclass, field

from session_ops.ops import registry
from session_ops.shared import discord, http
from session_ops.shared.state import write_json
from session_ops.zendesk import claude_cli

ALERTED_MARKER = "alerted"
LAST_ALERT = "last-alert.json"
# Discord answers in a second; past this the OnFailure backstop reports the run instead,
# and a job with a short timeout still has room for its alert.
ALERT_SECONDS = 45
# Present on a host rehearsing production: jobs publish beside it (env.rehearsing).
REHEARSAL_MARKER = "/etc/session-ops/rehearsal"


def rehearsal_marker():
    """True or False, or the reason it cannot tell. Not being able to tell stops the run:
    publishing for real by mistake is what the marker exists to prevent."""
    try:
        os.stat(REHEARSAL_MARKER)
    except FileNotFoundError:
        return False
    except OSError as exc:
        return f"cannot read {REHEARSAL_MARKER} ({exc.strerror}), so cannot tell whether this host rehearses"
    return True
ERROR_CHARS = 300
SECRET_NAME = re.compile(r"TOKEN|SECRET|KEY|WEBHOOK|PASSWORD|SEED", re.IGNORECASE)
# The bare path too: urllib3 quotes a failed request's URL without its host.
WEBHOOK_URL = re.compile(r"(?:https?://[^\s/]+)?/api/(?:v\d+/)?webhooks/\S+")

_step = None


def step(name):
    """Name what the job is doing now, for the alert should it fail during it."""
    global _step
    _step = name
    print(f"== {name}", flush=True)


@dataclass
class Outcome:
    """What a job with several targets returns: each target's error, or None.

    A failed target fails the run unless it is `optional`, in which case the run
    still alerts but succeeds, provided that alert reached Discord.
    """
    summary: str = ""
    targets: dict = field(default_factory=dict)
    optional: frozenset = frozenset()

    def failures(self):
        return {name: error for name, error in self.targets.items() if error}

    def fatal(self):
        return any(name not in self.optional for name in self.failures())


def scrub(text, environ=None):
    """`text` with every secret-looking environment value, and any webhook URL or
    path, replaced by its name."""
    environ = os.environ if environ is None else environ
    for name, value in environ.items():
        if SECRET_NAME.search(name) and value and len(value) >= 6:
            text = text.replace(value, f"<{name}>")
    return WEBHOOK_URL.sub("<webhook>", text)


def one_sentence(text):
    """`text` on one line, clipped: a pretty-printed error body cut at its first
    newline says only `{`."""
    flat = " ".join(str(text).split())
    return flat if len(flat) <= ERROR_CHARS else flat[:ERROR_CHARS - 1] + "…"


def describe(exc):
    status = getattr(exc, "http_status", None)
    if status is not None and hasattr(exc, "context"):  # crowdin-api-client's errors
        from session_ops.crowdin import sdk
        return f"Crowdin {status}: {sdk.error_message(exc)}"
    return f"{type(exc).__name__}: {exc}"


def alert_message(job, host, current_step, error, outcome):
    if error:
        during = f" during **{current_step}**" if current_step else ""
        lines = [f"❌ **{job.name}** failed on `{host}`{during}.", f"> {error}"]
    else:
        failed = len(outcome.failures())
        lines = [f"❌ **{job.name}** on `{host}`: {failed} of "
                 f"{len(outcome.targets)} targets failed."]
    for name, result in outcome.targets.items():
        mark = "✅" if not result else ("⚠️" if name in outcome.optional else "❌")
        lines.append(f"{mark} **{name}**" + (f": {result}" if result else ""))
    journal = f"`journalctl -u session-ops@{job.name} -n 50 --no-pager`"
    account = claude_cli.advice(error, *outcome.targets.values())
    if account:
        lines += [*account, journal]
    else:
        lines.append(f"{journal} · re-run: `systemctl start session-ops@{job.name}.service`")
    return "\n".join(lines)


def post_alert(job, message):
    webhook = os.environ.get(job.channel_env) or os.environ.get("ALERT_DISCORD_WEBHOOK_URL")
    if not webhook:
        print("No alert webhook in the environment; leaving it to the OnFailure backstop.",
              file=sys.stderr)
        return False
    payload = {"content": message, "allowed_mentions": {"parse": []}}
    session = http.Session(deadline=ALERT_SECONDS)
    return discord.post_to_discord(session, webhook, [payload]) == 1


def mark_alerted(state_dir):
    invocation = os.environ.get("INVOCATION_ID")
    if not invocation:
        return
    try:
        with open(os.path.join(state_dir, ALERTED_MARKER), "w", encoding="utf-8") as handle:
            handle.write(invocation)
            # The job's UMask makes it 0600; the backstop reads it as another account.
            os.fchmod(handle.fileno(), 0o644)
    except OSError as exc:
        print(f"Could not record the alert ({exc}); the backstop may repeat it.",
              file=sys.stderr)


def repeated_alert(state_dir, message, hours, now):
    """Whether `message` was reported less than `hours` ago; records it when not.

    Recorded whether or not the post lands: the backstop reports what it cannot."""
    path = os.path.join(state_dir, LAST_ALERT)
    try:
        with open(path, encoding="utf-8") as handle:
            last = json.load(handle)
        if last["message"] == message and now - last["at"] < hours * 3600:
            return True
    except (OSError, ValueError, KeyError, TypeError):
        pass
    try:
        write_json(path, {"message": message, "at": now})
    except OSError as exc:
        print(f"Could not record the alert ({exc}); the next failure alerts again.",
              file=sys.stderr)
    return False


def forget_alert(state_dir):
    try:
        os.remove(os.path.join(state_dir, LAST_ALERT))
    except FileNotFoundError:
        pass
    except OSError as exc:
        print(f"Could not clear {LAST_ALERT} ({exc}).", file=sys.stderr)


def call_target(function, argv):
    """Run one entry point, returning None, or its failure as one sentence.

    For a job with several targets: each one's failure is recorded rather than
    stopping the rest.
    """
    return _invoke(function, argv)[1]


def _invoke(function, argv):
    try:
        return function(argv), None
    except SystemExit as exc:
        if exc.code in (None, 0):
            return None, None
        print(exc.code, file=sys.stderr)
        return None, one_sentence(exc.code if isinstance(exc.code, str)
                                  else f"exited {exc.code}")
    except Exception as exc:  # the job's failure is the run's to report
        traceback.print_exc()
        return None, one_sentence(describe(exc))


def entry_point(job):
    module_name, function = job.entry.split(":")
    return getattr(importlib.import_module(module_name), function)


def file_away(path, folder):
    os.makedirs(folder, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    os.replace(path, os.path.join(folder, f"{stamp}-{os.path.basename(path)}"))


def run(job, dry_run=False, extra=()):
    """Run `job` and report its failure. Returns the process exit code."""
    global _step
    _step = None
    if not job.enabled:
        print(f"{job.name} is off (enabled = false in jobs.toml): nothing ran.")
        return 0
    state_dir = os.environ.get("STATE_DIRECTORY", "").split(":")[0] or job.state_dir
    missing = [name for name in job.env if not os.environ.get(name)]
    work = tempfile.mkdtemp(prefix=f"{job.name}-")
    os.environ["SESSION_OPS_WORK_DIR"] = work
    rehearsal = rehearsal_marker()
    if rehearsal is True:
        os.environ["SESSION_OPS_REHEARSAL"] = "1"
        print("Rehearsal: publishing to rehearsal/ branches and writing nothing to tickets.")
    try:
        if missing:
            _step = "checking the environment"
            outcome, error = Outcome(), f"missing {', '.join(missing)} in the environment"
        elif rehearsal not in (True, False):
            _step = "checking for a rehearsal"
            outcome, error = Outcome(), rehearsal
        else:
            try:
                function = entry_point(job)
            except Exception as exc:  # a broken install, reported like any failure
                traceback.print_exc()
                _step = "loading the job"
                outcome, error = Outcome(), one_sentence(describe(exc))
            else:
                result, error = _invoke(function, job.argv(dry_run, state_dir) + list(extra))
                outcome = result if isinstance(result, Outcome) else Outcome()
    finally:
        shutil.rmtree(work, ignore_errors=True)
    if outcome.summary:
        print(outcome.summary)
    if not error and not outcome.failures():
        if job.repeat_alert_hours and not dry_run:
            forget_alert(state_dir)
        return 0

    message = scrub(alert_message(job, socket.gethostname(), _step, error, outcome))
    code = 1 if error or outcome.fatal() else 0
    if dry_run:
        print(message)
        return code
    if (code and job.repeat_alert_hours
            and repeated_alert(state_dir, message, job.repeat_alert_hours, time.time())):
        print(f"{message}\nAlready reported within {job.repeat_alert_hours:g}h; not posted.")
        mark_alerted(state_dir)
        return code
    if not post_alert(job, message):
        # Even an optional target's failure then fails the unit: OnFailure= is all
        # that is left to report it.
        return 1
    if code:
        mark_alerted(state_dir)
    return code


def ready(job):
    """Whether the job is on, and every env file it reads exists and has something in it."""
    return job.enabled and all(os.path.isfile(path) and os.path.getsize(path) > 0
                               for path in job.env_files)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="session-ops", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    list_parser = sub.add_parser("list", help="Every registered job, its schedule and account.")
    readiness = list_parser.add_mutually_exclusive_group()
    readiness.add_argument("--ready", action="store_true",
                           help="Only the names of the jobs a timer, the queue or a path unit "
                                "starts, that are on and whose env files have content.")
    readiness.add_argument("--not-ready", action="store_true",
                           help="Only the names of those jobs that are on but whose env file "
                                "is empty.")
    readiness.add_argument("--timers", action="store_true",
                           help="Only the names of the ready jobs with a timer of their own.")
    readiness.add_argument("--off", action="store_true",
                           help="Only the names of the jobs jobs.toml turns off.")
    readiness.add_argument("--queued", action="store_true",
                           help="Only the names of the queued jobs, in the order they run.")
    readiness.add_argument("--path-units", action="store_true",
                           help="Only the names of the jobs a request from Discord starts.")
    run_parser = sub.add_parser("run", help="Run a job as its timer does.")
    run_parser.add_argument("job")
    run_parser.add_argument("--dry-run", action="store_true",
                            help="The job's own dry run; an alert is printed, not posted.")
    units_parser = sub.add_parser("units", help="Write each job's systemd drop-ins.")
    units_parser.add_argument("--out", required=True, metavar="DIR")
    sub.add_parser("discord-register", help="Register the Discord slash commands, from "
                                            "discord.env's variables.")
    argv = sys.argv[1:] if argv is None else list(argv)
    extra = argv[argv.index("--") + 1:] if "--" in argv else []
    args = parser.parse_args(argv[:argv.index("--")] if "--" in argv else argv)

    if args.command == "list":
        queue = registry.load_queue()
        if args.queued:
            print("\n".join(queue.jobs))
            return
        if args.path_units:
            print("\n".join(job.name for job in registry.load() if job.discord))
            return
        if args.off:
            print("\n".join(job.name for job in registry.load() if not job.enabled))
            return
        for job in registry.load():
            started = job.scheduled or job.discord
            if args.timers:
                if job.schedule and not job.queued and ready(job):
                    print(job.name)
            elif args.ready or args.not_ready:
                if started and job.enabled and ready(job) == args.ready:
                    print(job.name)
            else:
                when = f"queue: {queue.schedule}" if job.queued else job.schedule or "on demand"
                print(f"{job.name:24} {when:44} {job.user}")
        return
    if args.command == "units":
        from session_ops.ops import units
        for path in units.write(registry.load(), registry.load_queue(), args.out):
            print(path)
        return
    if args.command == "discord-register":
        from session_ops.ops import discord_commands
        discord_commands.main()
        return
    try:
        job = registry.get(args.job)
    except KeyError:
        sys.exit(f"No job named {args.job!r}; `session-ops list` shows them.")
    sys.exit(run(job, args.dry_run, extra))


if __name__ == "__main__":
    main()
