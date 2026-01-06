import os
import json
import argparse
from pathlib import Path
from datetime import datetime
from typing import Dict, Any
from generate_shared import (
    load_parsed_translations,
    clean_string,
    print_progress,
    print_success,
    run_main
)

AUTO_REPLACE_STATIC_STRINGS = False

# It seems that Xcode uses different language codes and doesn't support all of the languages we get from Crowdin
# (at least in the variants that Crowdin is specifying them in) so need to map/exclude them in order to build correctly
LANGUAGE_MAPPING = {
    'kmr': 'ku-TR',         # Explicitly Kurmanji in Türkiye, `ku-TR` is the general language code for Kurdish in Türkiye
    'no': 'nb-NO',          # Norwegian general, `nb-NO` is Norwegian Bokmål in Norway and is apparently seen as the standard
    'sr-CS': 'sr-Latn',     # Serbian (Latin)
    'tl': None,             # Tagalog (not supported, we have Filipino which might have to be enough for now)
}


def get_mapped_language(lang_id: str) -> str | None:
    """Map a language ID to its iOS equivalent, or return None if unsupported."""
    if lang_id in LANGUAGE_MAPPING:
        return LANGUAGE_MAPPING[lang_id]
    return lang_id


def convert_placeholders_for_plurals(forms: Dict[str, str], glossary_dict: Dict[str, str]) -> Dict[str, str]:
    """Replace {count} with %lld for iOS plural forms."""
    converted = {}
    for form, value in forms.items():
        converted[form] = clean_string(
            value,
            False,
            glossary_dict if AUTO_REPLACE_STATIC_STRINGS else {},
            {'{count}': '%lld'}
        )
    return converted


def sort_dict_case_insensitive(data):
    if isinstance(data, dict):
        return {k: sort_dict_case_insensitive(v) for k, v in sorted(data.items(), key=lambda item: item[0].lower())}
    elif isinstance(data, list):
        return [sort_dict_case_insensitive(i) for i in data]
    else:
        return data


def build_string_catalog(parsed_data: Dict[str, Any], glossary_dict: Dict[str, str]) -> Dict[str, Any]:
    """
    Build an Xcode String Catalog from pre-parsed translation data.
    Args:
        parsed_data: The pre-parsed translation data
        glossary_dict: Dictionary of non-translatable strings
    Returns:
        String catalog dictionary ready for JSON serialization
    """
    string_catalog = {
        "sourceLanguage": "en",
        "strings": {},
        "version": "1.0"
    }

    locales = parsed_data['locales']
    # Build list of languages with mapped IDs, filtering out unsupported ones
    languages_with_mapping = []
    for locale, locale_data in locales.items():
        lang_info = locale_data['language_info']
        mapped_id = get_mapped_language(lang_info['id'])
        if mapped_id is not None:
            languages_with_mapping.append({
                'locale': locale,
                'mapped_id': mapped_id,
                'data': locale_data
            })

    # Sort languages alphabetically by mapped_id (Xcode does this)
    languages_with_mapping.sort(key=lambda x: x['mapped_id'])

    for lang in languages_with_mapping:
        locale = lang['locale']
        target_language = lang['mapped_id']
        translations = lang['data']['translations']
        print_progress(f"Converting translations for {target_language} to target format...")

        for resname, trans_data in translations.items():
            if resname not in string_catalog["strings"]:
                string_catalog["strings"][resname] = {
                    "extractionState": "manual",
                    "localizations": {}
                }

            if trans_data['type'] == 'plural':
                forms = trans_data['forms']
                converted_forms = convert_placeholders_for_plurals(forms, glossary_dict)

                contains_count = any('{count}' in value for value in forms.values())
                if contains_count:
                    # Standard plural using {count}
                    variations = {
                        "plural": {
                            form: {
                                "stringUnit": {
                                    "state": "translated",
                                    "value": value
                                }
                            } for form, value in converted_forms.items()
                        }
                    }
                    string_catalog["strings"][resname]["localizations"][target_language] = {
                        "variations": variations
                    }
                else:
                    # Custom format using substitutions
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
                                        } for form, value in converted_forms.items()
                                    }
                                }
                            }
                        }
                    }
            else:
                # Regular string
                value = trans_data['value']
                string_catalog["strings"][resname]["localizations"][target_language] = {
                    "stringUnit": {
                        "state": "translated",
                        "value": clean_string(
                            value,
                            False,
                            glossary_dict if AUTO_REPLACE_STATIC_STRINGS else {},
                            {}
                        )
                    }
                }

    return sort_dict_case_insensitive(string_catalog)


def write_string_catalog(string_catalog: Dict[str, Any], output_dir: str):
    """Write the string catalog to disk."""
    output_file = os.path.join(output_dir, 'Localizable.xcstrings')
    os.makedirs(output_dir, exist_ok=True)

    with open(output_file, 'w', encoding='utf-8') as f:
        # Add spaces around ':' to match Xcode's format
        json.dump(string_catalog, f, ensure_ascii=False, indent=2, separators=(',', ' : '))


def generate_swift_constants(glossary_dict: Dict[str, str], output_paths: list):
    """Generate Swift file with non-translatable string constants."""
    for path in output_paths:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        filename = os.path.basename(path)
        enum_name, _ = os.path.splitext(filename)

        with open(path, 'w', encoding='utf-8') as file:
            file.write(f'// Copyright © {datetime.now().year} Rangeproof Pty Ltd. All rights reserved.\n')
            file.write('// This file is automatically generated and maintained, do not manually edit it.\n')
            file.write('//\n')
            file.write('// stringlint:disable\n')
            file.write('\n')
            file.write(f'public enum {enum_name} {{\n')

            for key, text in glossary_dict.items():
                cleaned_text = clean_string(text, False, glossary_dict, {})
                file.write(f'    public static let {key}: String = "{cleaned_text}"\n')

            file.write('}\n')


def main():
    parser = argparse.ArgumentParser(
        description='Convert parsed translations to Apple String Catalog.'
    )
    parser.add_argument(
        'parsed_translations_file',
        help='Path to the parsed translations JSON file'
    )
    parser.add_argument(
        'translations_output_directory',
        help='Directory to save the converted translation files'
    )
    parser.add_argument(
        'non_translatable_strings_output_paths',
        nargs='+',
        help='Paths to save the non-translatable strings to'
    )
    args = parser.parse_args()

    parsed_data = load_parsed_translations(args.parsed_translations_file)
    glossary_dict = parsed_data['glossary']

    print_progress("Generating static strings file...")
    generate_swift_constants(glossary_dict, args.non_translatable_strings_output_paths)
    print_success("Static string generation complete")

    print_progress("Converting translations to target format...")
    string_catalog = build_string_catalog(parsed_data, glossary_dict)
    write_string_catalog(string_catalog, args.translations_output_directory)
    print_success("All conversions complete")


if __name__ == "__main__":
    run_main(main)
