import importlib.util
import unittest

# session_util is a deb, not a wheel (see README, Community Bans). CI's sogs job
# installs it and checks the import first, so this skip cannot hide a broken suite.
if importlib.util.find_spec("session_util") is None:
    raise unittest.SkipTest("session_util is not installed")
