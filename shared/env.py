import os
import sys


def get_env(name, cli_value=None, required=True):
    if cli_value:
        return cli_value
    value = os.environ.get(name)
    if value:
        return value
    if required:
        sys.exit(f"Missing required config: set the {name} environment variable (or pass the matching flag).")
    return None
