"""Entry points for the runner's tests."""
from session_ops.ops.runner import Outcome, step

calls = []


def succeeds(argv):
    calls.append(argv)


def exits(argv):
    step("posting")
    raise SystemExit("Posted 0 of 2 messages.")


def raises(argv):
    step("downloading")
    raise ValueError(f"bad token {argv[-1]}\nsecond line")


def partial(argv):
    return Outcome(targets={"ios": None, "android": "push rejected"})


def optional_failure(argv):
    return Outcome(targets={"resolver": "500", "digest": None}, optional=frozenset({"resolver"}))


def exits_with_a_pretty_printed_body(argv):
    raise SystemExit("GitHub 401 on /orgs: {\n  \"message\": \"Bad credentials\"\n}")


LOGIN_EXPIRED = "claude exited 1 on classification: OAuth token has expired · Please run /login"


def digest_login_expired(argv):
    return Outcome(targets={"resolve reviews": None, "digest": LOGIN_EXPIRED},
                   optional=frozenset({"resolve reviews"}))


def exits_login_expired(argv):
    step("classifying")
    raise SystemExit(LOGIN_EXPIRED)
