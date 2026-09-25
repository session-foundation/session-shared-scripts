"""
    python -m unittest discover        # from crowdin/
    UPDATE_GOLDENS=1 python -m unittest test_goldens   # accept new output

The recordings are shaped like Crowdin's API responses rather than captured live.
"""
import contextlib
import io
import json
import os
import runpy
import sys
import tempfile
import unittest
from unittest import mock

import colorama
import requests

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
import report_multiple_translations as report  # noqa: E402
from shared.testing import RecordedSession, assert_golden, load_golden_json  # noqa: E402


def argv_for(recording, tmp):
    return [arg.replace("{tmp}", tmp) for arg in recording["argv"]]


class TestReportGolden(unittest.TestCase):
    def test_dry_run_for_one_locale(self):
        recording = load_golden_json("report/responses.json")
        session = RecordedSession(recording["exchanges"])
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(report.requests, "Session", lambda: session), \
                    mock.patch.object(sys, "argv", ["report", *argv_for(recording, tmp)]), \
                    contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                report.main()
            with open(os.path.join(tmp, "findings.json"), encoding="utf-8") as handle:
                findings = handle.read()
        assert_golden(self, "report/dry-run.json", out.getvalue())
        assert_golden(self, "report/findings.json", findings + "\n")


class TestDownloadGolden(unittest.TestCase):
    def test_export_payloads_and_written_files(self):
        recording = load_golden_json("download/responses.json")
        session = RecordedSession(recording["exchanges"])
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(requests, "get", session.get), \
                    mock.patch.object(requests, "post", session.post), \
                    mock.patch.object(sys, "argv", ["download", *argv_for(recording, tmp)]), \
                    contextlib.redirect_stdout(io.StringIO()) as out:
                try:
                    runpy.run_path(os.path.join(HERE, "download_translations_from_crowdin.py"),
                                   run_name="__main__")
                except SystemExit as exc:
                    self.fail(f"download exited {exc.code}:\n{out.getvalue()}")
                finally:
                    colorama.deinit()
            files = {}
            for name in sorted(os.listdir(tmp)):
                with open(os.path.join(tmp, name), encoding="utf-8") as handle:
                    files[name] = handle.read()
        exports = sorted((json.loads(data) for method, _, _, data in session.calls
                          if method == "POST"), key=lambda body: body["targetLanguageId"])
        assert_golden(self, "download/output.json",
                      json.dumps({"exports": exports, "files": files}, indent=2,
                                 ensure_ascii=False) + "\n")


if __name__ == "__main__":
    unittest.main()
