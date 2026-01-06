from typing import Dict, List, Any
import html
import json
import os
import sys
from colorama import Fore, Style


def print_progress(message: str):
    print(f"\033[2K{Fore.WHITE}⏳ {message}{Style.RESET_ALL}", end='\r')


def print_success(message: str):
    print(f"\033[2K{Fore.GREEN}✅ {message}{Style.RESET_ALL}")


def print_error(message: str):
    print(f"\033[2K{Fore.RED}❌ {message}{Style.RESET_ALL}")


def print_warning(message: str):
    print(f"\033[2K{Fore.YELLOW}⚠️  {message}{Style.RESET_ALL}")


def run_main(main_func):
    try:
        main_func()
    except KeyboardInterrupt:
        print("\nProcess interrupted by user")
        sys.exit(0)
    except Exception as e:
        print_error(f"An error occurred: {str(e)}")
        sys.exit(1)


def ensure_file_exists(file_path: str, description: str = "file"):
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"Could not find {description}: '{file_path}'")


def load_parsed_translations(input_file: str) -> Dict[str, Any]:
    """
    Load the pre-parsed translations JSON file.

    Args:
        input_file: Path to the parsed translations JSON file

    Returns:
        Dictionary containing parsed translation data
    """
    ensure_file_exists(input_file, "parsed translations file")

    with open(input_file, 'r', encoding='utf-8') as f:
        return json.load(f)


def clean_string(text: str, is_android: bool, glossary_dict: Dict[str, str], extra_replace_dict: Dict[str, str]):
    if is_android:
        # Note: any changes done for all platforms needs most likely to be done on crowdin side.
        # So we don't want to replace -&gt; with → for instance, we want the crowdin strings to not have those at all.
        # We can use standard XML escaped characters for most things (since XLIFF is an XML format) but
        # want the following cases escaped in a particular way (for android only)
        text = text.replace("'", r"\'")
        text = text.replace("&quot;", "\"")
        text = text.replace("\"", "\\\"")
        text = text.replace("&lt;b&gt;", "<b>")
        text = text.replace("&lt;/b&gt;", "</b>")
        text = text.replace("&lt;/br&gt;", "\\n")
        text = text.replace("<br/>", "\\n")
        text = text.replace("&lt;span&gt;",  '<font color="0">')
        text = text.replace("&lt;/span&gt;", '</font>')
        text = text.replace("<span>",        '<font color="0">')
        text = text.replace("</span>",       '</font>')
        # Assume any remaining ampersands are desired
        text = text.replace("&", "&amp;")
    else:
        text = html.unescape(text)          # Unescape any HTML escaping

    text = text.strip()               # Strip whitespace

    # replace all the defined constants (from crowdin's glossary) in the string
    for glossary_key in glossary_dict:
        text = text.replace("{" + glossary_key + "}",
                            glossary_dict[glossary_key])

    # if extra_replace_dict has keys, replace those too
    for extra_key in extra_replace_dict:
        text = text.replace(extra_key, extra_replace_dict[extra_key])
    return text


def load_glossary_dict(input_file: str) -> Dict[str, str]:
    ensure_file_exists(input_file, "glossary file")

    # Process the non-translatable string input
    non_translatable_strings_data = {}
    with open(input_file, 'r', encoding="utf-8") as file:
        non_translatable_strings_data = json.load(file)

    non_translatable_strings_entries = non_translatable_strings_data['data']
    glossary_dict = {
        entry['data']['note']: entry['data']['text']
        for entry in non_translatable_strings_entries
    }

    return glossary_dict


def setup_generation(input_directory: str):
    # Extract the project information
    print_progress("Processing project info...")
    project_info_file = os.path.join(input_directory, "_project_info.json")
    ensure_file_exists(project_info_file, "project info file")

    project_details = {}
    with open(project_info_file, 'r', encoding="utf-8") as file:
        project_details = json.load(file)

    non_translatable_strings_file = os.path.join(
        input_directory, "_non_translatable_strings.json")

    # Extract the language info and sort the target languages alphabetically by locale
    source_language: str = project_details['data']['sourceLanguage']
    target_languages: List[str] = project_details['data']['targetLanguages']
    target_languages.sort(key=lambda x: x['locale'])
    num_languages = len(target_languages)
    print_success(f"Project info processed, {
                  num_languages} languages will be converted")

    # Convert the non-translatable strings to the desired format
    rtl_languages: List[str] = [
        lang for lang in target_languages if lang["textDirection"] == "rtl"]

    return {
        "source_language": source_language,
        "rtl_languages": rtl_languages,
        "non_translatable_strings_file": non_translatable_strings_file,
        "target_languages": target_languages
    }
