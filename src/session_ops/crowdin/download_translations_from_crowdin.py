import os
import json
import sys
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock

from colorama import Fore, Style, init

from session_ops.crowdin import sdk
from session_ops.shared import http


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description='Download translations from Crowdin.')
    parser.add_argument('api_token', help='Crowdin API token')
    parser.add_argument('project_id', help='Crowdin project ID')
    parser.add_argument('download_directory', help='Directory to save the initial downloaded files')
    parser.add_argument('--glossary_id', help='Crowdin glossary ID (optional)', default=None)
    parser.add_argument('--concept_id', help='Crowdin non-translatable terms concept ID (optional)', default=None)
    parser.add_argument('--skip-untranslated-strings', action='store_true', help='Exclude strings which have not been translated from the translation files')
    parser.add_argument('--force-allow-unapproved', action='store_true', help='Include unapproved translations in the translation files')
    parser.add_argument('--max-workers', type=int, default=10, help='Maximum number of parallel downloads (default: 10, max: 20 due to Crowdin API limits)')
    parser.add_argument('-v', '--verbose', action='store_true', help='Enable verbose output')
    return parser.parse_args(argv)


def configure(args, session=None):
    global CROWDIN_API_TOKEN, CROWDIN_PROJECT_ID, CROWDIN_GLOSSARY_ID, CROWDIN_CONCEPT_ID
    global DOWNLOAD_DIRECTORY, SKIP_UNTRANSLATED_STRINGS, FORCE_ALLOW_UNAPPROVED, VERBOSE
    global MAX_WORKERS, CLIENT, FILES
    CROWDIN_API_TOKEN = args.api_token
    CROWDIN_PROJECT_ID = args.project_id
    CROWDIN_GLOSSARY_ID = args.glossary_id
    CROWDIN_CONCEPT_ID = args.concept_id
    DOWNLOAD_DIRECTORY = args.download_directory
    SKIP_UNTRANSLATED_STRINGS = args.skip_untranslated_strings
    FORCE_ALLOW_UNAPPROVED = args.force_allow_unapproved
    VERBOSE = args.verbose
    # Crowdin API limit is 20 simultaneous requests per account, and each worker
    # has one in flight at a time.
    MAX_WORKERS = min(args.max_workers, 20)
    CLIENT = sdk.client(CROWDIN_API_TOKEN, CROWDIN_PROJECT_ID, attempts=MAX_ATTEMPTS,
                        timeout=REQUEST_TIMEOUT_S, session=session)
    # The export URLs are presigned: the Crowdin token must not travel with them.
    FILES = session or http.Session(attempts=MAX_ATTEMPTS, timeout=REQUEST_TIMEOUT_S)


REQUEST_TIMEOUT_S = 30
MAX_ATTEMPTS = 5

progress_lock = Lock()
completed_count = 0
total_count = 0


def crowdin_call(context, call, **kwargs):
    try:
        return call(**kwargs)
    except sdk.APIException as exc:
        raise Exception(f"{context}: {sdk.error_message(exc)} (Code: {exc.http_status})") from exc


def download_file(url: str, output_path: str):
    response = FILES.get(url, stream=True)
    response.raise_for_status()

    with open(output_path, 'wb') as f:
        for chunk in response.iter_content(chunk_size=8192):
            f.write(chunk)


def export_and_download_language(language: dict, is_source: bool = False) -> str:
    global completed_count

    lang_id = language['id']
    lang_locale = language['locale']
    export_payload = {
        "targetLanguageId": lang_id,
        "format": "xliff",
        "skipUntranslatedStrings": False if is_source else SKIP_UNTRANSLATED_STRINGS,
        "exportApprovedOnly": False if is_source else (not FORCE_ALLOW_UNAPPROVED)
    }

    exported = crowdin_call(f"Export failed for {lang_locale}",
                            CLIENT.translations.export_project_translation, **export_payload)

    download_url = exported['data']['url']
    download_path = os.path.join(DOWNLOAD_DIRECTORY, f"{lang_locale}.xliff")
    download_file(download_url, download_path)

    with progress_lock:
        completed_count += 1
        print(f"\033[2K{Fore.WHITE}⏳ Downloaded {
              completed_count}/{total_count} translations...{Style.RESET_ALL}", end='\r')

    return lang_locale


def download():
    global total_count, completed_count
    # Retrieve the list of languages
    print(f"{Fore.WHITE}⏳ Retrieving project details...{Style.RESET_ALL}", end='\r')
    project_response = crowdin_call("Failed to retrieve project details",
                                    CLIENT.projects.get_project)
    project_details = project_response['data']
    source_language = project_details['sourceLanguage']
    target_languages = project_details['targetLanguages']
    num_languages = len(target_languages)
    print(f"\033[2K{Fore.GREEN}✅ Project details retrieved, found {num_languages} translations{Style.RESET_ALL}")

    if VERBOSE:
        print(f"{Fore.BLUE}Response: {json.dumps(project_response, indent=2)}{Style.RESET_ALL}")

    if not os.path.exists(DOWNLOAD_DIRECTORY):
        os.makedirs(DOWNLOAD_DIRECTORY)

    project_info_file = os.path.join(DOWNLOAD_DIRECTORY, "_project_info.json")
    with open(project_info_file, 'w', encoding='utf-8') as file:
        json.dump(project_response, file, indent=2)

    all_languages = [{'language': source_language, 'is_source': True}]
    for lang in sorted(target_languages, key=lambda x: x['locale']):
        all_languages.append({'language': lang, 'is_source': False})

    total_count = len(all_languages)
    completed_count = 0

    print(f"{Fore.WHITE}⏳ Downloading {total_count} translations (using {MAX_WORKERS} parallel workers)...{Style.RESET_ALL}")
    failed_languages = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_to_lang = {
            executor.submit(
                export_and_download_language,
                item['language'],
                item['is_source']
            ): item['language']['locale']
            for item in all_languages
        }

        for future in as_completed(future_to_lang):
            lang_locale = future_to_lang[future]
            try:
                future.result()
            except Exception as e:
                failed_languages.append((lang_locale, str(e)))
                if VERBOSE:
                    print(f"\n{Fore.RED}❌ Failed: {lang_locale} - {e}{Style.RESET_ALL}")

    if failed_languages:
        print(f"\033[2K{Fore.RED}❌ {len(failed_languages)} downloads failed:{Style.RESET_ALL}")
        for locale, error in failed_languages:
            print(f"  - {locale}: {error}")
        sys.exit(f"{len(failed_languages)} of {total_count} downloads failed, "
                 f"first {failed_languages[0][0]}: {failed_languages[0][1]}")
    else:
        print(f"\033[2K{Fore.GREEN}✅ Downloaded {total_count} translations complete{Style.RESET_ALL}")

    # Download non-translatable terms (if requested)
    if CROWDIN_GLOSSARY_ID is not None and CROWDIN_CONCEPT_ID is not None:
        print(f"{Fore.WHITE}⏳ Retrieving non-translatable strings...{Style.RESET_ALL}", end='\r')
        static_string_response = crowdin_call(
            "Failed to retrieve non-translatable strings", CLIENT.glossaries.list_terms,
            glossaryId=CROWDIN_GLOSSARY_ID, conceptId=CROWDIN_CONCEPT_ID, limit=500)

        if VERBOSE:
            print(f"{Fore.BLUE}Response: {json.dumps(static_string_response, indent=2)}{Style.RESET_ALL}")

        non_translatable_strings_file = os.path.join(DOWNLOAD_DIRECTORY, "_non_translatable_strings.json")
        with open(non_translatable_strings_file, 'w', encoding='utf-8') as file:
            json.dump(static_string_response, file, indent=2)

        print(f"\033[2K{Fore.GREEN}✅ Downloading non-translatable complete{Style.RESET_ALL}")


def main(argv=None, session=None):
    """`session` answers every request instead of the network, for tests."""
    init(autoreset=True)
    configure(parse_args(argv), session)
    try:
        download()
    except KeyboardInterrupt:
        print(f"\n{Fore.RED}Process interrupted by user{Style.RESET_ALL}")
        sys.exit(0)
    except Exception as e:
        print(f"\033[2K{Fore.RED}❌ An error occurred: {e}{Style.RESET_ALL}")
        sys.exit(f"Download failed: {e}")


if __name__ == "__main__":
    main()
