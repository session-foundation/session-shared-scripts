"""The Claude API as the Zendesk jobs call it: one schema-enforced request,
the model, and a failure worth quoting.
"""
import functools
import json
import sys

import anthropic


# Every Zendesk call uses the newest Opus, resolved once per process from the Models
# API, so a new Opus is picked up with no change here; the digest's clusters and
# severity calibration can shift with it.
OPUS_PREFIX = "claude-opus-"

# The model's ceiling: a full digest chunk's findings and its thinking share it. Only a
# streamed request may ask for this much; the SDK refuses a non-streaming one.
MAX_OUTPUT_TOKENS = 128000

# The API's error messages do not echo the prompt, but this log must never carry ticket
# text, so nothing here is passed through whole.
FAILURE_CHARS = 300

API_PREFIX = "Claude API"


@functools.cache
def client():
    """Authenticates with ANTHROPIC_API_KEY from the environment."""
    return anthropic.Anthropic()


@functools.cache
def model():
    """The id of the newest Opus the key can use."""
    try:
        opus = [m for m in client().models.list() if m.id.startswith(OPUS_PREFIX)]
    except anthropic.APIError as exc:
        sys.exit(failure(exc, "the model lookup"))
    if not opus:
        sys.exit(f"{API_PREFIX} lists no {OPUS_PREFIX}* model for this key.")
    return max(opus, key=lambda m: m.created_at).id


def failure(exc, label):
    """The log line for a failed request. advice() matches on its wording."""
    message = (getattr(exc, "message", None) or str(exc)).strip()[:FAILURE_CHARS]
    if isinstance(exc, anthropic.APITimeoutError):
        return f"{API_PREFIX} did not finish {label} in time."
    if isinstance(exc, (anthropic.AuthenticationError, anthropic.PermissionDeniedError)):
        return f"{API_PREFIX} refused the key on {label}: {message}"
    if isinstance(exc, anthropic.APIStatusError):
        return f"{API_PREFIX} returned {exc.status_code} on {label}: {message}"
    return f"{API_PREFIX} was unreachable on {label}: {message}"


def claude_json(effort, system_prompt, schema, prompt, timeout, label, model_id=None):
    """Run one schema-enforced Claude request on model_id, by default the newest Opus.
    Returns the parsed payload.

    Shared by the digest's classification and note_reply.py's composing: same request
    shape, same error semantics, one place to keep them right. Every failure exits
    with a line starting API_PREFIX, which is what advice() reads.
    """
    model_id = model_id or model()
    try:
        with client().with_options(timeout=timeout).messages.stream(
            model=model_id,
            max_tokens=MAX_OUTPUT_TOKENS,
            system=system_prompt,
            messages=[{"role": "user", "content": prompt}],
            output_config={
                "effort": effort,
                "format": {"type": "json_schema", "schema": schema},
            },
        ) as stream:
            response = stream.get_final_message()
    except anthropic.APIError as exc:
        sys.exit(failure(exc, label))

    if response.stop_reason == "refusal":
        sys.exit(f"{API_PREFIX} refused {label} "
                 f"({getattr(response.stop_details, 'category', None)!r}).")
    # A truncated answer is JSON that stops mid-object; the parse below would report a
    # baffling syntax error for something whose only fix is a smaller batch.
    if response.stop_reason == "max_tokens":
        sys.exit(f"{API_PREFIX} ran out of output tokens on {label}, so the JSON is "
                 f"incomplete. Ask for less per call; for the digest that is a lower "
                 f"--batch-size.")
    raw = "".join(block.text for block in response.content if block.type == "text")
    if not raw:
        sys.exit(f"{API_PREFIX} returned no text on {label}.")
    try:
        return json.loads(raw)
    except ValueError as exc:
        sys.exit(f"{API_PREFIX} result on {label} is not the JSON the schema asked "
                 f"for ({exc}).")


# A dead key reads as a broken job unless the alert names it: the job is fine and
# re-running it fixes nothing. Only a line this module wrote is checked: Zendesk's own
# 401 body says "Couldn't authenticate you".
AUTH_SIGNATURES = ("refused the key",)
# An account out of credit has a valid key, so rotating it fixes nothing either.
BILLING_SIGNATURES = ("credit balance",)
ENV_FILE = "/etc/session-ops/zendesk.env"


def _api_failure(line, signatures):
    line = line or ""
    return line.startswith(API_PREFIX) and any(s in line.lower() for s in signatures)


def is_auth_failure(line):
    return _api_failure(line, AUTH_SIGNATURES)


def is_billing_failure(line):
    return _api_failure(line, BILLING_SIGNATURES)


def advice(*lines):
    """What an alert says instead of a re-run hint when the Claude account is the
    problem, or [] when re-running might help."""
    if any(is_auth_failure(line) for line in lines):
        return ["**The Claude API key was refused.** Re-running the unit will not fix "
                f"it — put a valid `ANTHROPIC_API_KEY` in `{ENV_FILE}`, then restart "
                "zendesk-relay."]
    if any(is_billing_failure(line) for line in lines):
        return ["**The Claude API account is out of credit.** Re-running the unit will "
                "not fix it until the account is topped up."]
    return []
