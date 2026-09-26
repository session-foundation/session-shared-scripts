"""`session-ops`: run a registered job the way its timer does, list them, write units.

    session-ops list
    session-ops run github-prs-digest [--dry-run] [-- further job arguments]
    session-ops units --out /etc/systemd/system

A run owns what every job would otherwise repeat: checking the environment it needs,
a scratch directory, and telling Discord when it fails. The alert names the job, the
host, the step it was on, one sentence of error with secrets scrubbed, each target's
result for a job with several, and the commands to read the journal and re-run it.
The traceback goes to the journal only.

Having alerted, a run records its systemd invocation id, so the OnFailure= backstop
(session-ops-alert) knows this failure was already reported and stays quiet. The
backstop is for the failures a run cannot report itself: killed, timed out, or
Discord unreachable.
"""
import argparse
import importlib
import os
import re
import shutil
import socket
import sys
import tempfile
import traceback
from dataclasses import dataclass, field

from session_ops.ops import registry
from session_ops.shared import discord, http
from session_ops.zendesk import claude_cli

ALERTED_MARKER = "alerted"
ERROR_CHARS = 300
SECRET_NAME = re.compile(r"TOKEN|SECRET|KEY|WEBHOOK|PASSWORD|SEED", re.IGNORECASE)
WEBHOOK_URL = re.compile(r"https://(?:\w+\.)?discord(?:app)?\.com/api/webhooks/\S+")

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
    still alerts but succeeds.
    """
    summary: str = ""
    targets: dict = field(default_factory=dict)
    optional: frozenset = frozenset()

    def failures(self):
        return {name: error for name, error in self.targets.items() if error}

    def fatal(self):
        return any(name not in self.optional for name in self.failures())


def scrub(text, environ=None):
    """`text` with every secret-looking environment value, and any Discord webhook
    URL, replaced by its name."""
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
    if any(claude_cli.is_auth_failure(text) for text in (error, *outcome.targets.values())):
        lines += [*claude_cli.relogin_advice(), journal]
    else:
        lines.append(f"{journal} · re-run: `systemctl start session-ops@{job.name}.service`")
    return "\n".join(lines)


def post_alert(job, message):
    webhook = os.environ.get("ALERT_DISCORD_WEBHOOK_URL") or os.environ.get(job.channel_env)
    if not webhook:
        print("No alert webhook in the environment; leaving it to the OnFailure backstop.",
              file=sys.stderr)
        return False
    payload = {"content": message, "allowed_mentions": {"parse": []}}
    return discord.post_to_discord(http.Session(), webhook, [payload]) == 1


def mark_alerted(state_dir):
    invocation = os.environ.get("INVOCATION_ID")
    if not invocation:
        return
    try:
        with open(os.path.join(state_dir, ALERTED_MARKER), "w", encoding="utf-8") as handle:
            handle.write(invocation)
    except OSError as exc:
        print(f"Could not record the alert ({exc}); the backstop may repeat it.",
              file=sys.stderr)


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


def call(job, argv):
    """Run the job's entry point. Returns (outcome, error)."""
    module_name, function = job.entry.split(":")
    result, error = _invoke(getattr(importlib.import_module(module_name), function), argv)
    return (result if isinstance(result, Outcome) else Outcome()), error


def run(job, dry_run=False, extra=()):
    """Run `job` and report its failure. Returns the process exit code."""
    global _step
    _step = None
    state_dir = os.environ.get("STATE_DIRECTORY", "").split(":")[0] or job.state_dir
    missing = [name for name in job.env if not os.environ.get(name)]
    work = tempfile.mkdtemp(prefix=f"{job.name}-")
    os.environ["SESSION_OPS_WORK_DIR"] = work
    try:
        if missing:
            _step = "checking the environment"
            outcome, error = Outcome(), f"missing {', '.join(missing)} in the environment"
        else:
            outcome, error = call(job, job.argv(dry_run, state_dir) + list(extra))
    finally:
        shutil.rmtree(work, ignore_errors=True)
    if outcome.summary:
        print(outcome.summary)
    if not error and not outcome.failures():
        return 0

    message = scrub(alert_message(job, socket.gethostname(), _step, error, outcome))
    code = 1 if error or outcome.fatal() else 0
    if dry_run:
        print(message)
        return code
    if post_alert(job, message) and code:
        mark_alerted(state_dir)
    return code


def ready(job):
    """Whether every env file the job reads exists and has something in it."""
    return all(os.path.isfile(path) and os.path.getsize(path) > 0 for path in job.env_files)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="session-ops", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    list_parser = sub.add_parser("list", help="Every registered job, its schedule and account.")
    readiness = list_parser.add_mutually_exclusive_group()
    readiness.add_argument("--ready", action="store_true",
                           help="Only the names of scheduled jobs whose env files have content.")
    readiness.add_argument("--not-ready", action="store_true",
                           help="Only the names of scheduled jobs with an empty env file.")
    run_parser = sub.add_parser("run", help="Run a job as its timer does.")
    run_parser.add_argument("job")
    run_parser.add_argument("--dry-run", action="store_true",
                            help="The job's own dry run; an alert is printed, not posted.")
    units_parser = sub.add_parser("units", help="Write each job's systemd drop-ins.")
    units_parser.add_argument("--out", required=True, metavar="DIR")
    argv = sys.argv[1:] if argv is None else list(argv)
    extra = argv[argv.index("--") + 1:] if "--" in argv else []
    args = parser.parse_args(argv[:argv.index("--")] if "--" in argv else argv)

    if args.command == "list":
        for job in registry.load():
            if args.ready or args.not_ready:
                if job.schedule and ready(job) == args.ready:
                    print(job.name)
            else:
                print(f"{job.name:24} {job.schedule or 'on demand':38} {job.user}")
        return
    if args.command == "units":
        from session_ops.ops import units
        for path in units.write(registry.load(), args.out):
            print(path)
        return
    try:
        job = registry.get(args.job)
    except KeyError:
        sys.exit(f"No job named {args.job!r}; `session-ops list` shows them.")
    sys.exit(run(job, args.dry_run, extra))


if __name__ == "__main__":
    main()
