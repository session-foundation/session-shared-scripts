import os
import json
import xml.etree.ElementTree as ET
import sys
import argparse
import html
from pathlib import Path
from colorama import Fore, Style
from datetime import datetime
from generate_shared import load_glossary_dict, clean_string, setup_generation


# It seems that Xcode uses different language codes and doesn't support all of the languages we get from Crowdin
# (at least in the variants that Crowdin is specifying them in) so need to map/exclude them in order to build correctly
LANGUAGE_MAPPING = {
    'kmr': 'ku-TR',         # Explicitly Kurmanji in Türkiye, `ku-TR` is the general language code for Kurdish in Türkiye
    'no': 'nb-NO',          # Norwegian general, `nb-NO` is Norwegian Bokmål in Norway and is apparently seen as the standard
    'sr-CS': 'sr-Latn',     # Serbian (Latin)
    'tl': None,             # Tagalog (not supported, we have Filipino which might have to be enough for now)
}

# Parse command-line arguments
parser = argparse.ArgumentParser(description='Convert a XLIFF translation files to Apple String Catalog.')
parser.add_argument('raw_translations_directory', help='Directory which contains the raw translation files')
parser.add_argument('translations_output_directory', help='Directory to save the converted translation files')
parser.add_argument('non_translatable_strings_output_path', help='Path to save the non-translatable strings to')
args = parser.parse_args()

INPUT_DIRECTORY = args.raw_translations_directory
TRANSLATIONS_OUTPUT_DIRECTORY = args.translations_output_directory
NON_TRANSLATABLE_STRINGS_OUTPUT_PATH = args.non_translatable_strings_output_path

def filter_and_map_language_ids(target_languages):
    result = []
    for lang in target_languages:
        if lang['id'] in LANGUAGE_MAPPING:
            mapped_value = LANGUAGE_MAPPING[lang['id']]
            if mapped_value is not None:
                lang['mapped_id'] = mapped_value
                result.append(lang)
        else:
            lang['mapped_id'] = lang['id']
            result.append(lang)
    return result

def parse_xliff(file_path):
    tree = ET.parse(file_path)
    root = tree.getroot()
    namespace = {'ns': 'urn:oasis:names:tc:xliff:document:1.2'}
    translations = {}

    file_elem = root.find('ns:file', namespaces=namespace)
    if file_elem is None:
        raise ValueError(f"Invalid XLIFF structure in file: {file_path}")

    target_language = file_elem.get('target-language')
    if target_language is None:
        raise ValueError(f"Missing target-language in file: {file_path}")

    if target_language in LANGUAGE_MAPPING:
        target_language = LANGUAGE_MAPPING[target_language]

    # Handle plural groups first (want to make sure any warnings shown are correctly attributed to plurals or non-plurals)
    for group in root.findall('.//ns:group[@restype="x-gettext-plurals"]', namespaces=namespace):
        plural_forms = {}
        resname = None
        for trans_unit in group.findall('ns:trans-unit', namespaces=namespace):
            if resname is None:
                resname = trans_unit.get('resname') or trans_unit.get('id')

            target = trans_unit.find('ns:target', namespaces=namespace)
            source = trans_unit.find('ns:source', namespaces=namespace)
            context_group = trans_unit.find('ns:context-group', namespaces=namespace)

            if context_group is not None:
                plural_form = context_group.find('ns:context[@context-type="x-plural-form"]', namespaces=namespace)
                if plural_form is not None:
                    form = plural_form.text.split(':')[-1].strip().lower()

                    if target is not None and target.text:
                        plural_forms[form] = target.text
                    elif source is not None and source.text:
                        # If target is missing or empty, use source as a fallback
                        plural_forms[form]  = source.text
                        print(f"Warning: Using source text for plural form '{form}' of '{resname}' in '{target_language}' as target is missing or empty")

        if resname and plural_forms:
            translations[resname] = plural_forms

    # Then handle non-plurals (ignore any existing values as they are plurals)
    for trans_unit in root.findall('.//ns:trans-unit', namespaces=namespace):
        resname = trans_unit.get('resname') or trans_unit.get('id')
        if resname is None or resname in translations:
            continue  # Skip entries without a resname/id and entries which already exist (ie. plurals)

        target = trans_unit.find('ns:target', namespaces=namespace)
        source = trans_unit.find('ns:source', namespaces=namespace)

        if target is not None and target.text:
            translations[resname] = target.text
        elif source is not None and source.text:
            # If target is missing or empty, use source as a fallback
            translations[resname] = source.text
            print(f"Warning: Using source text for '{resname}' in '{target_language}' as target is missing or empty")

    return translations, target_language

def convert_placeholders_for_plurals(translations):
    # Replace {count} with %lld for iOS
    converted_translations = {}
    for form, value in translations.items():
        converted_translations[form] = clean_string(value, False, {}, {'{count}': '%lld'})

    return converted_translations

def sort_dict_case_insensitive(data):
    if isinstance(data, dict):
        return {k: sort_dict_case_insensitive(v) for k, v in sorted(data.items(), key=lambda item: item[0].lower())}
    elif isinstance(data, list):
        return [sort_dict_case_insensitive(i) for i in data]
    else:
        return data

def convert_xliff_to_string_catalog(input_dir, output_dir, source_language, target_languages):
    string_catalog = {
        "sourceLanguage": "en",
        "strings": {},
        "version": "1.0"
    }
    target_mapped_languages = filter_and_map_language_ids(target_languages)
    source_language['mapped_id'] = source_language['id']

    # We need to sort the full language list (if the source language comes first rather than in alphabetical order
    # then the output will differ from what Xcode generates)
    all_languages = [source_language] + target_mapped_languages
    sorted_languages = sorted(all_languages, key=lambda x: x['mapped_id'])

    for language in sorted_languages:
        lang_locale = language['locale']
        input_file = os.path.join(input_dir, f"{lang_locale}.xliff")

        if not os.path.exists(input_file):
            raise FileNotFoundError(f"Could not find '{input_file}' in raw translations directory")

        try:
            translations, target_language = parse_xliff(input_file)
        except Exception as e:
            raise ValueError(f"Error processing locale {lang_locale}: {str(e)}")

        print(f"\033[2K{Fore.WHITE}⏳ Converting translations for {target_language} to target format...{Style.RESET_ALL}", end='\r')

        for resname, translation in translations.items():
            if resname not in string_catalog["strings"]:
                string_catalog["strings"][resname] = {
                    "extractionState": "manual",
                    "localizations": {}
                }

            if isinstance(translation, dict):  # It's a plural group
                converted_translations = convert_placeholders_for_plurals(translation)

                # Check if any of the translations contain '{count}'
                contains_count = any('{count}' in value for value in translation.values())

                if contains_count:
                    # It's a standard plural which the code can switch off of using `{count}`
                    variations = {
                        "plural": {
                            form: {
                                "stringUnit": {
                                    "state": "translated",
                                    "value": value
                                }
                            } for form, value in converted_translations.items()
                        }
                    }
                    string_catalog["strings"][resname]["localizations"][target_language] = {"variations": variations}
                else:
                    # Otherwise we need to use a custom format which uses just the `{count}` and replaces it with an entire string
                    string_catalog["strings"][resname]["localizations"][target_language] = {
                        "stringUnit": {
                            "state": "translated",
                            "value": "%#@arg1@"
                        },
                        "substitutions": {
                            "arg1": {
                                "argNum": 1,
                                "formatSpecifier": "lld",
                                "variations": {
                                    "plural": {
                                        form: {
                                            "stringUnit": {
                                                "state": "translated",
                                                "value": value
                                            }
                                        } for form, value in converted_translations.items()
                                    }
                                }
                            }
                        }
                    }
            else:
                string_catalog["strings"][resname]["localizations"][target_language] = {
                    "stringUnit": {
                        "state": "translated",
                        "value": clean_string(translation, False, {}, {})
                    }
                }

    # Note: Xcode sorts the strings in a case insensitive way so do the same here, apparently some versions of
    # Python won't maintain insertion order once a dict is manipulated so we need to finalise the dict and then
    # generate a correctly sorted one to be saved to disk
    sorted_string_catalog = sort_dict_case_insensitive(string_catalog)

    output_file = os.path.join(output_dir, 'Localizable.xcstrings')
    os.makedirs(output_dir, exist_ok=True)

    with open(output_file, 'w', encoding='utf-8') as f:
        # We need to add spaces around the `:` in the output beacuse Xcode inserts one when opening
        # the `xcstrings` so if we don't then there is an absurd number of diffs...
        json.dump(sorted_string_catalog, f, ensure_ascii=False, indent=2, separators=(',', ' : '))

def convert_non_translatable_strings_to_swift(input_file, output_path):
    glossary_dict = load_glossary_dict(input_file)

    # Output the file in the desired format
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    with open(output_path, 'w', encoding='utf-8') as file:
        file.write(f'// Copyright © {datetime.now().year} Rangeproof Pty Ltd. All rights reserved.\n')
        file.write('// This file is automatically generated and maintained, do not manually edit it.\n')
        file.write('//\n')
        file.write('// stringlint:disable\n')
        file.write('\n')
        file.write('public enum Constants {\n')
        for key in glossary_dict:
            text = glossary_dict[key]
            file.write(f'    public static let {key}: String = "{text}"\n')

        file.write('}\n')

def convert_all_files(input_directory):
    setup_values = setup_generation(input_directory)
    source_language, rtl_languages, non_translatable_strings_file, target_languages = setup_values.values()
    print(f"\033[2K{Fore.WHITE}⏳ Generating static strings file...{Style.RESET_ALL}", end='\r')

    convert_non_translatable_strings_to_swift(non_translatable_strings_file, NON_TRANSLATABLE_STRINGS_OUTPUT_PATH)
    print(f"\033[2K{Fore.GREEN}✅ Static string generation complete{Style.RESET_ALL}")

    # Convert the XLIFF data to the desired format
    print(f"\033[2K{Fore.WHITE}⏳ Converting translations to target format...{Style.RESET_ALL}", end='\r')
    convert_xliff_to_string_catalog(input_directory, TRANSLATIONS_OUTPUT_DIRECTORY, source_language, target_languages)
    print(f"\033[2K{Fore.GREEN}✅ All conversions complete{Style.RESET_ALL}")

if __name__ == "__main__":
    try:
        convert_all_files(INPUT_DIRECTORY)
    except KeyboardInterrupt:
        print("\nProcess interrupted by user")
        sys.exit(0)
    except Exception as e:
        print(f"\033[2K{Fore.RED}❌ An error occurred: {str(e)}")
        sys.exit(1)
