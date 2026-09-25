"""Run with: cd crowdin && python -m unittest discover"""
import json
import os
import sys
import tempfile
import unittest

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import crowdin_sdk  # noqa: E402
import download_translations_from_crowdin as download  # noqa: E402
from shared.testing import FakeResponse, FakeSession, NoSleep, NonJsonResponse  # noqa: E402

API = "https://api.crowdin.com/api/v2"


class StreamResponse(FakeResponse):
    def __init__(self, body):
        super().__init__({})
        self.body = body

    def iter_content(self, chunk_size):
        yield self.body


def client(api_responses=(), download_responses=()):
    crowdin = download.Crowdin("secret", "618696", max_workers=4)
    crowdin_sdk.use_session(crowdin.api, FakeSession(api_responses))
    crowdin.downloads = FakeSession(download_responses)
    return crowdin


def api_session(crowdin):
    return crowdin.api.get_api_requestor().session


def api_calls(crowdin):
    return api_session(crowdin).fixed.calls


class TestRequests(unittest.TestCase):
    def test_the_token_goes_to_the_api_and_never_to_the_export_host(self):
        crowdin = download.Crowdin("secret", "618696", max_workers=4)
        self.assertEqual(api_session(crowdin).headers["Authorization"], "Bearer secret")
        self.assertNotIn("Authorization", crowdin.downloads.headers)

    def test_a_server_error_is_retried(self):
        crowdin = client([FakeResponse({}, status_code=503), FakeResponse({"data": {"id": 1}})])
        with NoSleep():
            self.assertEqual(crowdin.call("project", crowdin.api.projects.get_project)["data"],
                             {"id": 1})
        self.assertEqual(len(api_calls(crowdin)), 2)
        self.assertEqual(api_calls(crowdin)[0][1], f"{API}/projects/618696")

    def test_a_client_error_names_the_step_and_crowdins_message(self):
        crowdin = client([FakeResponse({"error": {"message": "Token invalid"}}, status_code=401)])
        with self.assertRaises(download.CrowdinError) as caught:
            crowdin.call("Failed to retrieve project details", crowdin.api.projects.get_project)
        self.assertEqual(str(caught.exception),
                         "Failed to retrieve project details: Token invalid (Code: 401)")

    def test_a_non_json_error_page_still_reads_as_an_error(self):
        page = NonJsonResponse()
        page.status_code = 404
        crowdin = client([page])
        with self.assertRaises(download.CrowdinError) as caught:
            crowdin.call("project", crowdin.api.projects.get_project)
        self.assertIn("maintenance", str(caught.exception))
        self.assertIn("404", str(caught.exception))


class TestExport(unittest.TestCase):
    def test_a_language_is_exported_then_saved_under_its_locale(self):
        crowdin = client([FakeResponse({"data": {"url": "https://storage.example/x.xliff"}})],
                         [StreamResponse(b"<xliff/>")])
        with tempfile.TemporaryDirectory() as directory:
            locale = download.export_and_download_language(
                crowdin, {"id": "de", "locale": "de-DE"}, directory, is_source=False,
                skip_untranslated=True, allow_unapproved=False)
            with open(os.path.join(directory, "de-DE.xliff"), "rb") as handle:
                self.assertEqual(handle.read(), b"<xliff/>")
        self.assertEqual(locale, "de-DE")
        method, url, kwargs = api_calls(crowdin)[0]
        self.assertEqual((method.upper(), url),
                         ("POST", f"{API}/projects/618696/translations/exports"))
        self.assertEqual(json.loads(kwargs["data"]),
                         {"targetLanguageId": "de", "format": "xliff",
                          "skipUntranslatedStrings": True, "exportApprovedOnly": True})
        self.assertEqual(crowdin.downloads.calls[0][1], "https://storage.example/x.xliff")

    def test_the_source_language_is_always_exported_whole(self):
        crowdin = client([FakeResponse({"data": {"url": "https://storage.example/en.xliff"}})],
                         [StreamResponse(b"")])
        with tempfile.TemporaryDirectory() as directory:
            download.export_and_download_language(
                crowdin, {"id": "en", "locale": "en"}, directory, is_source=True,
                skip_untranslated=True, allow_unapproved=False)
        payload = json.loads(api_calls(crowdin)[0][2]["data"])
        self.assertFalse(payload["skipUntranslatedStrings"])
        self.assertFalse(payload["exportApprovedOnly"])

    def test_a_rejected_download_raises(self):
        crowdin = client([FakeResponse({"data": {"url": "https://storage.example/x.xliff"}})],
                         [FakeResponse({}, status_code=403)])
        with tempfile.TemporaryDirectory() as directory, self.assertRaises(requests.HTTPError):
            download.export_and_download_language(
                crowdin, {"id": "de", "locale": "de-DE"}, directory, is_source=False,
                skip_untranslated=False, allow_unapproved=True)


class TestArguments(unittest.TestCase):
    def test_the_workflow_invocation_still_parses(self):
        args = download.parse_args(["tok", "618696", "raw", "--glossary_id", "407522",
                                    "--concept_id", "36", "--skip-untranslated-strings"])
        self.assertEqual((args.api_token, args.project_id, args.download_directory),
                         ("tok", "618696", "raw"))
        self.assertEqual((args.glossary_id, args.concept_id), ("407522", "36"))
        self.assertTrue(args.skip_untranslated_strings)
        self.assertFalse(args.force_allow_unapproved)
        self.assertEqual(args.max_workers, 10)


if __name__ == "__main__":
    unittest.main()
