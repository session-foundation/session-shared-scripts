"""Byte-for-byte comparison of a job's output with tests/goldens/."""
import json
import os

GOLDENS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "goldens")


def load_golden_json(relpath):
    with open(os.path.join(GOLDENS_DIR, relpath), encoding="utf-8") as handle:
        return json.load(handle)


def assert_golden(case, relpath, actual):
    """Compare `actual` with tests/goldens/<relpath>; UPDATE_GOLDENS=1 rewrites it."""
    path = os.path.join(GOLDENS_DIR, relpath)
    if os.environ.get("UPDATE_GOLDENS"):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(actual)
        return
    with open(path, encoding="utf-8") as handle:
        expected = handle.read()
    case.maxDiff = None
    case.assertEqual(expected, actual, f"{relpath} differs (UPDATE_GOLDENS=1 to accept)")

