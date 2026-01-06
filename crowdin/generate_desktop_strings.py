import os
import json
import re
import argparse
from pathlib import Path
from typing import Dict, List, Any
from generate_shared import (
    load_parsed_translations,
    clean_string,
    print_progress,
    print_success,
    run_main
)

# Customizable mapping for output folder hierarchy
# Add entries here to customize the output path for specific locales
# Format: 'input_locale': 'output_path'
LOCALE_PATH_MAPPING = {
    'en-US': 'en',
    'kmr-TR': 'kmr',
    # Note: we don't want to replace - with _ anymore.
    # We still need those mappings, otherwise they fallback to their 2 letter codes
    'hy-AM': 'hy-AM',
    'es-419': 'es-419',
    'pt-BR': 'pt-BR',
    'pt-PT': 'pt-PT',
    'zh-CN': 'zh-CN',
    'zh-TW': 'zh-TW',
    'sr-CS': 'sr-CS',
    'sr-SP': 'sr-SP'
    # Add more mappings as needed
}


def matches_braced_pattern(string: str) -> bool:
    return re.search(r"\{(.+?)\}", string) is not None


def snake_to_camel(snake_str: str) -> str:
    parts = snake_str.split('_')
    return parts[0].lower() + ''.join(word.capitalize() for word in parts[1:])


def generate_icu_pattern(trans_data: Dict[str, Any] | str, glossary_dict: Dict[str, str]) -> str:
    """
    Generate an ICU pattern from translation data.
    Args:
        trans_data: Either a dict with 'type' and 'forms'/'value', or a raw string
        glossary_dict: Dictionary of non-translatable strings
    Returns:
        ICU-formatted string
    """
    # Handle raw strings (from glossary)
    if isinstance(trans_data, str):
        return clean_string(trans_data, False, glossary_dict, {})
    if trans_data['type'] == 'plural':
        pattern_parts = []
        for form, value in trans_data['forms'].items():
            if form in ['zero', 'one', 'two', 'few', 'many', 'other', 'exact', 'fractional']:
                cleaned_value = clean_string(value, False, glossary_dict, {})
                pattern_parts.append(f"{form} [{cleaned_value}]")
        return "{{count, plural, {0}}}".format(" ".join(pattern_parts))
    else:
        return clean_string(trans_data['value'], False, glossary_dict, {})


def get_output_locale(locale: str, two_letter_code: str) -> str:
    return LOCALE_PATH_MAPPING.get(locale, LOCALE_PATH_MAPPING.get(two_letter_code, two_letter_code))


def convert_locale_to_json(
    translations: Dict[str, Any],
    glossary_dict: Dict[str, str],
    output_dir: str,
    locale: str,
    two_letter_code: str
) -> str:
    """
    Convert translations for a single locale to JSON format.
    Args:
        translations: Dictionary of translations for this locale
        glossary_dict: Dictionary of non-translatable strings
        output_dir: Base output directory
        locale: Full locale code
        two_letter_code: Two-letter language code
    Returns:
        The output locale name used
    """
    sorted_translations = sorted(translations.items())
    converted_translations = {}

    for resname, trans_data in sorted_translations:
        converted_translations[resname] = generate_icu_pattern(trans_data, glossary_dict)

    # Add glossary items (converted to camelCase)
    for resname, text in glossary_dict.items():
        converted_translations[snake_to_camel(resname)] = generate_icu_pattern(text, glossary_dict)

    # Write output
    output_locale = get_output_locale(locale, two_letter_code)
    locale_output_dir = os.path.join(output_dir, output_locale)
    output_file = os.path.join(locale_output_dir, 'messages.json')
    os.makedirs(locale_output_dir, exist_ok=True)

    with open(output_file, 'w', encoding='utf-8') as file:
        json.dump(converted_translations, file, ensure_ascii=False, indent=2, sort_keys=True)
        file.write('\n\n')

    return output_locale


def generate_typescript_constants(
    glossary_dict: Dict[str, str],
    output_path: str,
    exported_locales: List[str],
    rtl_languages: List[Dict]
):
    rtl_locales = sorted([lang["twoLettersCode"] for lang in rtl_languages])

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    joined_exported_locales = ",".join(f"\n  '{locale}'" for locale in exported_locales)
    joined_rtl_locales = ", ".join(f"'{locale}'" for locale in rtl_locales)

    with open(output_path, 'w', encoding='utf-8') as file:
        file.write('export enum LOCALE_DEFAULTS {\n')

        for key, text in glossary_dict.items():
            if not matches_braced_pattern(text):
                cleaned_text = clean_string(text, False, glossary_dict, {})
                file.write(f"  {key} = '{cleaned_text}',\n")

        file.write('}\n\n')
        file.write(f"export const rtlLocales = [{joined_rtl_locales}];\n\n")
        file.write(f"export const crowdinLocales = [{joined_exported_locales},\n] as const;\n\n")
        file.write("export type CrowdinLocale = (typeof crowdinLocales)[number];\n\n")
        file.write('export function isCrowdinLocale(locale: string): locale is CrowdinLocale {\n')
        file.write('  return crowdinLocales.includes(locale as CrowdinLocale);\n')
        file.write('}\n\n')


def main():
    parser = argparse.ArgumentParser(description='Convert parsed translations to JSON.')
    parser.add_argument(
        '--qa_build',
        help='Set to true to output only English strings (only used for QA)',
        action=argparse.BooleanOptionalAction
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
        'non_translatable_strings_output_path',
        help='Path to save the non-translatable strings to'
    )
    args = parser.parse_args()
    is_qa_build = args.qa_build or False

    parsed_data = load_parsed_translations(args.parsed_translations_file)
    glossary_dict = parsed_data['glossary']
    source_language = parsed_data['source_language']
    target_languages = parsed_data['target_languages']
    rtl_languages = parsed_data['rtl_languages']
    locales = parsed_data['locales']

    print_progress("Converting translations to target format...")
    exported_locales = []

    languages_to_process = [source_language] + ([] if is_qa_build else target_languages)

    for language in languages_to_process:
        lang_locale = language['locale']
        lang_two_letter_code = language['twoLettersCode']

        print_progress(f"Converting translations for {lang_locale} to target format...")

        locale_data = locales.get(lang_locale)
        if locale_data is None:
            raise ValueError(f"Missing locale data for {lang_locale}")

        exported_as = convert_locale_to_json(
            locale_data['translations'],
            glossary_dict,
            args.translations_output_directory,
            lang_locale,
            lang_two_letter_code
        )
        exported_locales.append(exported_as)

    print_success("All conversions complete")

    print_progress("Generating static strings file...")
    generate_typescript_constants(
        glossary_dict,
        args.non_translatable_strings_output_path,
        exported_locales,
        rtl_languages
    )
    print_success("Static string generation complete")


if __name__ == "__main__":
    run_main(main)
