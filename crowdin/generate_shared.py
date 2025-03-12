from typing import Dict, List
import html
import json
import os
from colorama import Fore, Style


def clean_string(
    text: str,
    is_android: bool,
    glossary_dict: Dict[str, str],
    extra_replace_dict: Dict[str, str],
):
    if is_android:
        # Note: any changes done for all platforms needs most likely to be done on crowdin side.
        # So we don't want to replace -&gt; with → for instance, we want the crowdin strings to not have those at all.
        # We can use standard XML escaped characters for most things (since XLIFF is an XML format) but
        # want the following cases escaped in a particular way (for android only)
        text = text.replace("'", r"\'")
        text = text.replace("&quot;", '"')
        text = text.replace('"', '\\"')
        text = text.replace("&lt;b&gt;", "<b>")
        text = text.replace("&lt;/b&gt;", "</b>")
        text = text.replace("&lt;/br&gt;", "\\n")
        text = text.replace("<br/>", "\\n")
        text = text.replace("&", "&amp;")  # Assume any remaining ampersands are desired
    else:
        text = html.unescape(text)  # Unescape any HTML escaping

    text = text.strip()  # Strip whitespace

    # replace all the defined constants (from crowdin's glossary) in the string
    for glossary_key in glossary_dict:
        text = text.replace("{" + glossary_key + "}", glossary_dict[glossary_key])

    # if extra_replace_dict has keys, replace those too
    for extra_key in extra_replace_dict:
        text = text.replace(extra_key, extra_replace_dict[extra_key])
    return text


def load_glossary_dict(input_file: str) -> Dict[str, str]:
    if not os.path.exists(input_file):
        raise FileNotFoundError(f"Could not find '{input_file}' in raw translations directory")

    # Process the non-translatable string input
    non_translatable_strings_data = {}
    with open(input_file, "r", encoding="utf-8") as file:
        non_translatable_strings_data = json.load(file)

    non_translatable_strings_entries = non_translatable_strings_data["data"]
    glossary_dict = {entry["data"]["note"]: entry["data"]["text"] for entry in non_translatable_strings_entries}

    return glossary_dict


def setup_generation(input_directory: str):
    # Extract the project information
    print(f"\033[2K{Fore.WHITE}⏳ Processing project info...{Style.RESET_ALL}", end="\r")
    project_info_file = os.path.join(input_directory, "_project_info.json")
    if not os.path.exists(project_info_file):
        raise FileNotFoundError(f"Could not find '{project_info_file}' in raw translations directory")

    project_details = {}
    with open(project_info_file, "r", encoding="utf-8") as file:
        project_details = json.load(file)

    non_translatable_strings_file = os.path.join(input_directory, "_non_translatable_strings.json")

    # Extract the language info and sort the target languages alphabetically by locale
    source_language: str = project_details["data"]["sourceLanguage"]
    target_languages: List[str] = project_details["data"]["targetLanguages"]
    target_languages.sort(key=lambda x: x["locale"])
    num_languages = len(target_languages)
    print(f"\033[2K{Fore.GREEN}✅ Project info processed, {num_languages} languages will be converted{Style.RESET_ALL}")

    # Convert the non-translatable strings to the desired format
    rtl_languages: List[str] = [lang for lang in target_languages if lang["textDirection"] == "rtl"]

    return {
        "source_language": source_language,
        "rtl_languages": rtl_languages,
        "non_translatable_strings_file": non_translatable_strings_file,
        "target_languages": target_languages,
    }
