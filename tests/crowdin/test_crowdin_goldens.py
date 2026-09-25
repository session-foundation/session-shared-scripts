"""
    UPDATE_GOLDENS=1 uv run python -m unittest tests.crowdin.test_crowdin_goldens   # accept new output

The recordings are shaped like Crowdin's API responses rather than captured live.
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

import colorama

from session_ops.crowdin import download_translations_from_crowdin as download
from session_ops.crowdin import report_multiple_translations as report
from session_ops.crowdin import sdk
from session_ops.shared.testing import RecordedSession
from tests.golden import assert_golden, load_golden_json


def argv_for(recording, tmp):
    return [arg.replace("{tmp}", tmp) for arg in recording["argv"]]


class TestReportGolden(unittest.TestCase):
    def test_dry_run_for_one_locale(self):
        recording = load_golden_json("report/responses.json")
        session = RecordedSession(recording["exchanges"])
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(report, "crowdin_client",
                                      lambda token, pid: sdk.client(token, pid, session=session)), \
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
            with contextlib.redirect_stdout(io.StringIO()) as out:
                try:
                    download.main(argv_for(recording, tmp), session=session)
                except SystemExit as exc:
                    self.fail(f"download exited {exc.code}:\n{out.getvalue()}")
                finally:
                    colorama.deinit()
            files = {}
            for name in sorted(os.listdir(tmp)):
                with open(os.path.join(tmp, name), encoding="utf-8") as handle:
                    files[name] = handle.read()
        exports = sorted((json.loads(data) for method, _, _, data in session.calls
                          if method.upper() == "POST"), key=lambda body: body["targetLanguageId"])
        assert_golden(self, "download/output.json",
                      json.dumps({"exports": exports, "files": files}, indent=2,
                                 ensure_ascii=False) + "\n")


if __name__ == "__main__":
    unittest.main()
