import os
import sys


def get_env(name, override=None, required=True, default=None):
    """A flag's value if given, else the environment, else `default`.

    An empty value counts as unset. A required setting that is missing exits with
    the variable to set, so an unattended run fails on config before it spends
    anything.
    """
    if override:
        return override
    value = os.environ.get(name)
    if value:
        return value
    if default is not None:
        return default
    if required:
        sys.exit(f"Missing required config: set the {name} environment variable (or pass the matching flag).")
    return None
