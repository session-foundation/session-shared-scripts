import os
import json
import sys
import argparse
import requests

from colorama import Fore, Style, init

# Initialize colorama
init(autoreset=True)

# Parse command-line arguments
parser = argparse.ArgumentParser(description='Download translations from Crowdin.')
parser.add_argument('api_token', help='Crowdin API token')
parser.add_argument('project_id', help='Crowdin project ID')
parser.add_argument('download_directory', help='Directory to save the initial downloaded files')
parser.add_argument('--glossary_id', help='Crowdin glossary ID (optional)', default=None)
parser.add_argument('--concept_id', help='Crowdin non-translatable terms concept ID (optional)', default=None)
parser.add_argument('--skip-untranslated-strings', action='store_true', help='Exclude strings which have not been translated from the translation files')
parser.add_argument('--force-allow-unapproved', action='store_true', help='Include unapproved translations in the translation files')
parser.add_argument('-v', '--verbose', action='store_true', help='Enable verbose output')
args = parser.parse_args()

CROWDIN_API_BASE_URL = "https://api.crowdin.com/api/v2"
CROWDIN_API_TOKEN = args.api_token
CROWDIN_PROJECT_ID = args.project_id
CROWDIN_GLOSSARY_ID = args.glossary_id
CROWDIN_CONCEPT_ID = args.concept_id
DOWNLOAD_DIRECTORY = args.download_directory
SKIP_UNTRANSLATED_STRINGS = args.skip_untranslated_strings
FORCE_ALLOW_UNAPPROVED = args.force_allow_unapproved
VERBOSE = args.verbose

REQUEST_TIMEOUT_S = 5

def check_error(response):
    """
    Function to check for errors in API responses
    """
    if response.status_code != 200:
        print(f"\033[2K{Fore.RED}❌ Error: {response.json().get('error', {}).get('message', 'Unknown error')} (Code: {response.status_code}){Style.RESET_ALL}")
        if VERBOSE:
            print(f"{Fore.BLUE}Response: {json.dumps(response.json(), indent=2)}{Style.RESET_ALL}")
        sys.exit(1)

def download_file(url: str, output_path: str):
    """
    Function to download a file from Crowdin
    """
    response = requests.get(url, stream=True, timeout=REQUEST_TIMEOUT_S)
    response.raise_for_status()

    with open(output_path, 'wb') as f:
        for chunk in response.iter_content(chunk_size=8192):
            f.write(chunk)


def main():
    """
    Main Function
    Fetch crowdin project info, and iterate over each locale to save the corresponding .xliff locally.
    """
    # Retrieve the list of languages
    print(f"{Fore.WHITE}⏳ Retrieving project details...{Style.RESET_ALL}", end='\r')
    project_response = requests.get(f"{CROWDIN_API_BASE_URL}/projects/{CROWDIN_PROJECT_ID}",
                                      headers={"Authorization": f"Bearer {CROWDIN_API_TOKEN}"},
                                      timeout=REQUEST_TIMEOUT_S)
    check_error(project_response)
    project_details = project_response.json()['data']
    source_language_id = project_details['sourceLanguageId']
    source_language = project_details['sourceLanguage']
    target_languages = project_details['targetLanguages']
    num_languages = len(target_languages)
    print(f"\033[2K{Fore.GREEN}✅ Project details retrieved, found {num_languages} translations{Style.RESET_ALL}")

    if VERBOSE:
        print(f"{Fore.BLUE}Response: {json.dumps(project_response.json(), indent=2)}{Style.RESET_ALL}")

    # Ensure the download and output directories exist
    if not os.path.exists(DOWNLOAD_DIRECTORY):
        os.makedirs(DOWNLOAD_DIRECTORY)

    project_info_file = os.path.join(DOWNLOAD_DIRECTORY, "_project_info.json")
    with open(project_info_file, 'w', encoding='utf-8') as file:
        json.dump(project_response.json(), file, indent=2)

    # Retrieve the source language
    print(f"\033[2K{Fore.WHITE}⏳ Exporting source language {source_language_id}...{Style.RESET_ALL}", end='\r')
    source_lang_locale = source_language['locale']
    source_export_payload = {
        "targetLanguageId": source_language_id,
        "format": "xliff",
        "skipUntranslatedStrings": False,
        "exportApprovedOnly": False
    }
    source_export_response = requests.post(f"{CROWDIN_API_BASE_URL}/projects/{CROWDIN_PROJECT_ID}/translations/exports",
                                    headers={"Authorization": f"Bearer {CROWDIN_API_TOKEN}", "Content-Type": "application/json"},
                                    data=json.dumps(source_export_payload), timeout=REQUEST_TIMEOUT_S)
    check_error(source_export_response)

    if VERBOSE:
        print(f"\n{Fore.BLUE}Response: {json.dumps(source_export_response.json(), indent=2)}{Style.RESET_ALL}")

    # Download the exported file
    source_download_url = source_export_response.json()['data']['url']
    source_download_path = os.path.join(DOWNLOAD_DIRECTORY, f"{source_lang_locale}.xliff")
    print(f"\033[2K{Fore.WHITE}⏳ Downloading translations for {source_lang_locale}...{Style.RESET_ALL}", end='\r')
    try:
        download_file(source_download_url, source_download_path)
    except requests.exceptions.HTTPError as e:
        print(f"\033[2K{Fore.RED}❌ Failed to download translations for {source_lang_locale} (Error: {e}){Style.RESET_ALL}")
        if VERBOSE:
            print(f"{Fore.BLUE}Response: {e.response.text}{Style.RESET_ALL}")
        sys.exit(1)

    # Completed downloading
    print(f"\033[2K{Fore.GREEN}✅ Downloading source language complete{Style.RESET_ALL}")

    # Sort languages alphabetically by locale
    target_languages.sort(key=lambda x: x['locale'])

    # Iterate over each language and download the translations
    for index, language in enumerate(target_languages, start=1):
        lang_id = language['id']
        lang_locale = language['locale']
        prefix = f"({index:02d}/{num_languages:02d})"

        # Request export of translations for the specific language
        print(f"\033[2K{Fore.WHITE}⏳ {prefix} Exporting translations for {lang_locale}...{Style.RESET_ALL}", end='\r')
        export_payload = {
            "targetLanguageId": lang_id,
            "format": "xliff",
            "skipUntranslatedStrings": (True if SKIP_UNTRANSLATED_STRINGS else False),
            "exportApprovedOnly": (False if FORCE_ALLOW_UNAPPROVED else True)
        }
        export_response = requests.post(f"{CROWDIN_API_BASE_URL}/projects/{CROWDIN_PROJECT_ID}/translations/exports",
                                        headers={"Authorization": f"Bearer {CROWDIN_API_TOKEN}", "Content-Type": "application/json"},
                                        data=json.dumps(export_payload), timeout=REQUEST_TIMEOUT_S)
        check_error(export_response)

        if VERBOSE:
            print(f"\n{Fore.BLUE}Response: {json.dumps(export_response.json(), indent=2)}{Style.RESET_ALL}")

        # Download the exported file
        download_url = export_response.json()['data']['url']
        download_path = os.path.join(DOWNLOAD_DIRECTORY, f"{lang_locale}.xliff")
        print(f"\033[2K{Fore.WHITE}⏳ {prefix} Downloading translations for {lang_locale}...{Style.RESET_ALL}", end='\r')
        try:
            download_file(download_url, download_path)
        except requests.exceptions.HTTPError as e:
            print(f"\033[2K{Fore.RED}❌ {prefix} Failed to download translations for {lang_locale} (Error: {e}){Style.RESET_ALL}")
            if VERBOSE:
                print(f"{Fore.BLUE}Response: {e.response.text}{Style.RESET_ALL}")
            sys.exit(1)

    # Completed downloading
    print(f"\033[2K{Fore.GREEN}✅ Downloading {num_languages} translations complete{Style.RESET_ALL}")

    # Download non-translatable terms (if requested)
    if CROWDIN_GLOSSARY_ID is not None and CROWDIN_CONCEPT_ID is not None:
        print(f"{Fore.WHITE}⏳ Retrieving non-translatable strings...{Style.RESET_ALL}", end='\r')
        static_string_response = requests.get(f"{CROWDIN_API_BASE_URL}/glossaries/{CROWDIN_GLOSSARY_ID}/terms?conceptId={CROWDIN_CONCEPT_ID}&limit=500",
                                          headers={"Authorization": f"Bearer {CROWDIN_API_TOKEN}"},
                                          timeout=REQUEST_TIMEOUT_S)
        check_error(static_string_response)

        if VERBOSE:
            print(f"{Fore.BLUE}Response: {json.dumps(static_string_response.json(), indent=2)}{Style.RESET_ALL}")

        non_translatable_strings_file = os.path.join(DOWNLOAD_DIRECTORY, "_non_translatable_strings.json")
        with open(non_translatable_strings_file, 'w', encoding='utf-8') as file:
            json.dump(static_string_response.json(), file, indent=2)

        print(f"\033[2K{Fore.GREEN}✅ Downloading non-translatable complete{Style.RESET_ALL}")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print(f"\n{Fore.RED}Process interrupted by user{Style.RESET_ALL}")
        sys.exit(0)
