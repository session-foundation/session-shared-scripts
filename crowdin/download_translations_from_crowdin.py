#!/usr/bin/env python3
"""Download every language of a Crowdin project as XLIFF, plus its non-translatable terms.

Usage:
    download_translations_from_crowdin.py <api_token> <project_id> <download_directory>
        [--glossary_id ID --concept_id ID] [--skip-untranslated-strings]
        [--force-allow-unapproved] [--max-workers N] [-v]
"""
import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock, Semaphore

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from generate_shared import print_error, print_progress, print_success, run_main  # noqa: E402
from shared.retry import request_with_retry  # noqa: E402

API = "https://api.crowdin.com/api/v2"
# Crowdin allows 20 simultaneous requests per account.
MAX_CONCURRENT_REQUESTS = 20
REQUEST_TIMEOUT_S = 30
MAX_ATTEMPTS = 5


class CrowdinError(Exception):
    pass


def error_message(response):
    """Crowdin's error message, or the start of the body when it is not that envelope."""
    try:
        return response.json().get("error", {}).get("message", "Unknown error")
    except ValueError:
        return response.text[:200] or "Unknown error"


class Crowdin:
    """One project's API, gated to Crowdin's concurrency limit across the worker threads."""

    def __init__(self, token, project_id, max_workers):
        self.project_id = project_id
        self.session = requests.Session()
        self.session.headers["Authorization"] = f"Bearer {token}"
        # Exports are served from a signed URL on another host, which must not see the token.
        self.downloads = requests.Session()
        self.gate = Semaphore(min(max_workers, MAX_CONCURRENT_REQUESTS))

    def request(self, method, path, context, **kwargs):
        """One API response's JSON, or CrowdinError naming `context`."""
        with self.gate:
            response = request_with_retry(self.session, method, f"{API}/{path}",
                                          attempts=MAX_ATTEMPTS, timeout=REQUEST_TIMEOUT_S,
                                          **kwargs)
        if response.status_code != 200:
            raise CrowdinError(f"{context}: {error_message(response)} "
                               f"(Code: {response.status_code})")
        return response.json()

    def download(self, url, output_path):
        response = request_with_retry(self.downloads, "GET", url, attempts=MAX_ATTEMPTS,
                                      timeout=REQUEST_TIMEOUT_S, stream=True)
        response.raise_for_status()
        with open(output_path, "wb") as handle:
            for chunk in response.iter_content(chunk_size=8192):
                handle.write(chunk)


class Progress:
    def __init__(self, total):
        self.total, self.done, self.lock = total, 0, Lock()

    def tick(self):
        with self.lock:
            self.done += 1
            print_progress(f"Downloaded {self.done}/{self.total} translations...")


def export_and_download_language(client, language, directory, is_source, skip_untranslated,
                                 allow_unapproved):
    """Export one language and save it as <locale>.xliff. Returns the locale."""
    locale = language["locale"]
    payload = {
        "targetLanguageId": language["id"],
        "format": "xliff",
        "skipUntranslatedStrings": False if is_source else skip_untranslated,
        "exportApprovedOnly": False if is_source else not allow_unapproved,
    }
    export = client.request("POST", f"projects/{client.project_id}/translations/exports",
                            f"Export failed for {locale}", json=payload)
    client.download(export["data"]["url"], os.path.join(directory, f"{locale}.xliff"))
    return locale


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Download translations from Crowdin.")
    parser.add_argument("api_token", help="Crowdin API token")
    parser.add_argument("project_id", help="Crowdin project ID")
    parser.add_argument("download_directory", help="Directory to save the downloaded files")
    parser.add_argument("--glossary_id", help="Crowdin glossary ID (optional)")
    parser.add_argument("--concept_id", help="Crowdin non-translatable terms concept ID (optional)")
    parser.add_argument("--skip-untranslated-strings", action="store_true",
                        help="Exclude strings which have not been translated")
    parser.add_argument("--force-allow-unapproved", action="store_true",
                        help="Include unapproved translations")
    parser.add_argument("--max-workers", type=int, default=10,
                        help=f"Parallel downloads (default: 10, at most {MAX_CONCURRENT_REQUESTS})")
    parser.add_argument("-v", "--verbose", action="store_true", help="Print the API responses")
    return parser.parse_args(argv)


def main():
    args = parse_args()
    client = Crowdin(args.api_token, args.project_id, args.max_workers)

    print_progress("Retrieving project details...")
    project = client.request("GET", f"projects/{args.project_id}",
                             "Failed to retrieve project details")
    if args.verbose:
        print(json.dumps(project, indent=2))
    source_language = project["data"]["sourceLanguage"]
    target_languages = sorted(project["data"]["targetLanguages"], key=lambda x: x["locale"])
    print_success(f"Project details retrieved, found {len(target_languages)} translations")

    os.makedirs(args.download_directory, exist_ok=True)
    with open(os.path.join(args.download_directory, "_project_info.json"), "w",
              encoding="utf-8") as handle:
        json.dump(project, handle, indent=2)

    languages = [(source_language, True)] + [(lang, False) for lang in target_languages]
    workers = min(args.max_workers, MAX_CONCURRENT_REQUESTS)
    print(f"⏳ Downloading {len(languages)} translations (using {workers} parallel workers)...")
    progress = Progress(len(languages))
    failed = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(export_and_download_language, client, language,
                            args.download_directory, is_source, args.skip_untranslated_strings,
                            args.force_allow_unapproved): language["locale"]
            for language, is_source in languages}
        for future in as_completed(futures):
            try:
                future.result()
                progress.tick()
            except Exception as exc:  # one language's failure must not hide the others'
                failed.append((futures[future], str(exc)))
    if failed:
        print_error(f"{len(failed)} downloads failed:")
        for locale, error in sorted(failed):
            print(f"  - {locale}: {error}")
        sys.exit(1)
    print_success(f"Downloaded {len(languages)} translations complete")

    if args.glossary_id is not None and args.concept_id is not None:
        print_progress("Retrieving non-translatable strings...")
        terms = client.request(
            "GET", f"glossaries/{args.glossary_id}/terms",
            "Failed to retrieve non-translatable strings",
            params={"conceptId": args.concept_id, "limit": 500})
        if args.verbose:
            print(json.dumps(terms, indent=2))
        with open(os.path.join(args.download_directory, "_non_translatable_strings.json"), "w",
                  encoding="utf-8") as handle:
            json.dump(terms, handle, indent=2)
        print_success("Downloading non-translatable complete")


if __name__ == "__main__":
    run_main(main)
