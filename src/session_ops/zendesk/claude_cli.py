"""The Claude Code CLI as the Zendesk jobs call it: one schema-enforced request,
the model aliases, and a failure worth quoting.
"""
import json
import os
import subprocess
import sys


# Shorthands for the override, so ZENDESK_TRIAGE_MODEL=sonnet works for a big
# backfill without anyone looking up an id. The API takes ids only, so they are
# mapped here; each is the newest model in its family, and a full id passes through
# untouched.
API_MODEL_ALIASES = {
    "opus": "claude-opus-5",
    "sonnet": "claude-sonnet-5",
    "haiku": "claude-haiku-4-5",
}


# The classifier is the local Claude Code CLI rather than the Anthropic SDK, so
# authentication is whatever `claude` is already logged in as and no key lives here.
CLAUDE_CLI = "claude"


# Dropped from the CLI's environment. Each one silently outranks whatever `claude` is
# logged in as, and each is API-backend configuration — a box that once ran that way
# still has the key in its EnvironmentFile, where it is now dead config that would
# otherwise pick the credential, and the billing, for every classification.
#
# CLAUDE_CODE_OAUTH_TOKEN is deliberately not in this list. It is a subscription
# credential like the interactive login, not an API key, and it is the only one of
# these an unattended host can renew on a yearly rather than weekly cadence — see
# deploy/README.md. Nothing else can set it: it has never been written by anything
# this repo deploys, so it reaches the CLI only because somebody put it there.
CLAUDE_AUTH_OVERRIDES = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
)


# How much of a failed call's output reaches the log. The CLI can echo input back and
# this log must not carry ticket text, so nothing here is ever passed through whole.
CLI_FAILURE_CHARS = 300


def resolve_api_model(model):
    """Map a shorthand model name onto the id `claude --model` expects.

    Anything that isn't a known shorthand passes through untouched, so a pinned id
    (`claude-opus-4-8`) or a model newer than this table still works.
    """
    return API_MODEL_ALIASES.get(model, model)


def cli_failure_detail(stdout, stderr, limit=CLI_FAILURE_CHARS):
    """The readable half of a failed `claude --print` run, clipped for the log.

    stderr wins, but the failures that matter most — a refused login, an exhausted
    limit — leave it empty and put their message in the `--output-format json`
    envelope on stdout, where it sits behind enough usage boilerplate to survive no
    clip at all. Hence parsing the envelope rather than clipping it. `terminal_reason`
    rides along when it fits: it is what separates an auth failure from a limit.

    Output that is not that envelope is reported raw: a CLI that dies before emitting
    one has still said the only thing anybody will get.
    """
    detail = (stderr or "").strip()
    if detail:
        return detail[:limit]
    raw = (stdout or "").strip()
    try:
        envelope = json.loads(raw)
    except ValueError:
        envelope = None
    if isinstance(envelope, dict):
        message = ""
        for name in ("result", "error"):
            value = envelope.get(name)
            if isinstance(value, dict):
                value = value.get("message")
            if isinstance(value, str) and value.strip():
                message = value.strip()
                break
        reason = envelope.get("terminal_reason") or envelope.get("subtype")
        reason = reason.strip() if isinstance(reason, str) else ""
        if message and reason and len(message) + len(reason) + 3 <= limit:
            return f"{message} ({reason})"
        if message:
            return message[:limit]
        if reason:
            return f"it reported {reason!r} and no message."
    return raw[:limit]


def claude_cli_json(model, effort, system_prompt, schema, prompt, timeout, label):
    """Run one schema-enforced Claude Code request. Returns the parsed payload.

    Shared by the digest's classification and note_reply.py's composing: same flags,
    same error semantics, one place to keep them right.

    `--json-schema` enforces the schema the way the API's structured outputs did.
    Authentication is whatever `claude` is already logged in as, so neither caller
    holds a Claude key.

    The prompt goes over **stdin**, not argv. Linux caps one argument at 128KB
    (MAX_ARG_STRLEN) and a full --batch-size 400 chunk is around 685KB, so passing it
    as an argument would work on a normal day and die with "Argument list too long"
    on a backfill. It is also the more private channel: argv is world-readable
    through /proc, and these prompts carry ticket text.
    """
    command = [
        CLAUDE_CLI, "--print",
        "--model", model,
        "--effort", effort,
        "--system-prompt", system_prompt,
        "--json-schema", json.dumps(schema),
        "--output-format", "json",
        "--no-session-persistence",
        # Nothing outside this call may change what the model is told. The two flags
        # cover different halves of that and neither implies the other:
        # --setting-sources "" drops the user and project settings — and the hooks
        # inside them — while --tools "" removes the tools. Without the first, a
        # .claude/settings.json next to this file, or one in the service account's
        # home, silently joins every classification and every translation.
        #
        # --tools stays last: it is variadic, so it swallows any following argument
        # that does not begin with a dash.
        "--setting-sources", "",
        "--tools", "",
    ]
    child_env = {name: value for name, value in os.environ.items()
                 if name not in CLAUDE_AUTH_OVERRIDES}
    try:
        done = subprocess.run(command, input=prompt, capture_output=True, text=True,
                              check=False, timeout=timeout, env=child_env)
    except FileNotFoundError:
        sys.exit(f"{CLAUDE_CLI} is not on PATH. {label} runs through the Claude Code "
                 f"CLI, so it has to be installed and logged in.")
    except subprocess.TimeoutExpired:
        sys.exit(f"{CLAUDE_CLI} did not finish {label} within {timeout}s.")

    if done.returncode != 0:
        detail = cli_failure_detail(done.stdout, done.stderr)
        if not detail:
            detail = ("it printed nothing, which is what a login it can no longer "
                      "use looks like; check that `claude` is still signed in.")
        sys.exit(f"{CLAUDE_CLI} exited {done.returncode} on {label}: {detail}")
    try:
        response = json.loads(done.stdout)
    except ValueError as exc:
        sys.exit(f"{CLAUDE_CLI} returned output that is not JSON on {label} ({exc}).")
    if not isinstance(response, dict):
        sys.exit(f"{CLAUDE_CLI} returned {type(response).__name__} on {label}, "
                 f"expected an object.")
    # is_error and subtype are the CLI's signals for success; stop_reason deliberately
    # is not — a successful structured-output run reports "tool_use", because that is
    # how the schema is enforced underneath.
    if response.get("is_error") or response.get("subtype") != "success":
        detail = cli_failure_detail(done.stdout, done.stderr)
        sys.exit(f"{CLAUDE_CLI} reported failure on {label} "
                 f"(subtype={response.get('subtype')!r}, "
                 f"api_error_status={response.get('api_error_status')!r})"
                 f"{': ' + detail if detail else '.'}")
    # stop_reason is worth reading for this one value. There is no --max-tokens to
    # raise, so an answer too long to finish comes back as JSON that stops mid-object,
    # and the parse below would report a baffling syntax error for something whose
    # only fix is a smaller batch.
    if response.get("stop_reason") == "max_tokens":
        sys.exit(f"{CLAUDE_CLI} ran out of output tokens on {label}, so the JSON is "
                 f"incomplete. Ask for less per call; for the digest that is a lower "
                 f"--batch-size.")

    # structured_output is the object --json-schema produced, so it beats re-parsing
    # the `result` string: one less decode, and immune to prose alongside the JSON.
    payload = response.get("structured_output")
    if payload is not None:
        return payload
    raw = response.get("result")
    if not raw:
        sys.exit(f"{CLAUDE_CLI} returned neither structured_output nor a result "
                 f"on {label}.")
    try:
        return json.loads(raw)
    except ValueError as exc:
        sys.exit(f"{CLAUDE_CLI} result on {label} is not the JSON the schema asked "
                 f"for ({exc}).")
