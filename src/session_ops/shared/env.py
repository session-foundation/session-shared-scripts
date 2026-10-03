import os
import sys


def rehearsing():
    """Whether this run rehearses production: it publishes beside it and writes nothing
    to live tickets. The runner sets it while /etc/session-ops/rehearsal exists."""
    return os.environ.get("SESSION_OPS_REHEARSAL") == "1"


def get_env(name, cli_value=None, required=True):
    if cli_value:
        return cli_value
    value = os.environ.get(name)
    if value:
        return value
    if required:
        sys.exit(f"Missing required config: set the {name} environment variable (or pass the matching flag).")
    return None
