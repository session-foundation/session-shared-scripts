import os
import re
import argparse
from pathlib import Path
from typing import Dict, Any
from generate_shared import (
    load_parsed_translations,
    clean_string,
    print_progress,
    print_success,
    run_main
)

# Variables that should be treated as numeric (using %d)
NUMERIC_VARIABLES = ['count', 'found_count', 'total_count']

AUTO_REPLACE_STATIC_STRINGS = False


def convert_placeholders(text: str) -> str:
    """Convert {placeholder} syntax to Android positional format."""
    def repl(match):
        var_name = match.group(1)
        index = len(set(re.findall(r'\{([^}]+)\}', text[:match.start()]))) + 1

        if var_name in NUMERIC_VARIABLES:
            return f"%{index}$d"
        else:
            return f"%{index}$s"

    return re.sub(r'\{([^}]+)\}', repl, text)


def generate_android_xml(
    translations: Dict[str, Any],
    app_name: str | None,
    glossary_dict: Dict[str, str]
) -> str:
    """
    Generate Android strings.xml content from translations.
    Args:
        translations: Dictionary of translations
        app_name: App name to include (only for source language)
        glossary_dict: Dictionary for string cleaning
    Returns:
        XML string content
    """
    sorted_translations = sorted(translations.items())
    result = '<?xml version="1.0" encoding="utf-8"?>\n'
    result += '<resources>\n'

    if app_name is not None:
        result += f'    <string name="app_name" translatable="false">{app_name}</string>\n'

    for resname, trans_data in sorted_translations:
        if trans_data['type'] == 'plural':
            result += f'    <plurals name="{resname}">\n'
            for form, value in trans_data['forms'].items():
                escaped_value = clean_string(convert_placeholders(value), True, glossary_dict, {})
                result += f'        <item quantity="{form}">{escaped_value}</item>\n'
            result += '    </plurals>\n'
        else:
            # Regular strings: DON'T convert placeholders
            escaped_target = clean_string(trans_data['value'], True, glossary_dict, {})
            result += f'    <string name="{resname}">{escaped_target}</string>\n'

    result += '</resources>'
    return result


def write_android_xml(
    translations: Dict[str, Any],
    output_dir: str,
    source_locale: str,
    locale: str,
    glossary_dict: Dict[str, str]
):
    """Write Android strings.xml for a locale."""
    is_source_language = locale == source_locale
    app_name = glossary_dict.get('app_name')

    output_data = generate_android_xml(
        translations,
        app_name if is_source_language else None,
        glossary_dict if AUTO_REPLACE_STATIC_STRINGS else {}
    )

    # Android locale path format
    android_safe_locale = f"b+{locale.replace('-', '+')}"

    if is_source_language:
        language_output_dir = os.path.join(output_dir, 'values')
    else:
        language_output_dir = os.path.join(output_dir, f'values-{android_safe_locale}')

    os.makedirs(language_output_dir, exist_ok=True)
    language_output_file = os.path.join(language_output_dir, 'strings.xml')

    with open(language_output_file, 'w', encoding='utf-8') as file:
        file.write(output_data)


def generate_kotlin_constants(glossary_dict: Dict[str, str], output_path: str):
    """Generate Kotlin file with non-translatable string constants."""
    max_key_length = max(len(key) for key in glossary_dict)
    app_name = glossary_dict.get('app_name')

    if not app_name:
        raise ValueError("Could not find app_name in glossary_dict")

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    with open(output_path, 'w', encoding='utf-8') as file:
        file.write('package org.session.libsession.utilities\n\n')
        file.write('// Non-translatable strings for use with the UI\n')
        file.write("object NonTranslatableStringConstants {\n")
        for key_lowercase, text in glossary_dict.items():
            key = key_lowercase.upper()
            cleaned_text = clean_string(text, True, glossary_dict, {})
            file.write(f'    const val {key:<{max_key_length}} = "{cleaned_text}"\n')

        file.write('}\n\n')


def main():
    parser = argparse.ArgumentParser(
        description='Convert parsed translations to Android XML.'
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

    parsed_data = load_parsed_translations(args.parsed_translations_file)
    glossary_dict = parsed_data['glossary']
    source_language = parsed_data['source_language']
    target_languages = parsed_data['target_languages']
    locales = parsed_data['locales']

    print_progress("Generating static strings file...")
    generate_kotlin_constants(glossary_dict, args.non_translatable_strings_output_path)
    print_success("Static string generation complete")

    print_progress("Converting translations to target format...")
    source_locale = source_language['locale']

    for language in [source_language] + target_languages:
        lang_locale = language['locale']

        if lang_locale == 'sh-HR':
            # See https://en.wikipedia.org/wiki/Language_secessionism#In_Serbo-Croatian
            print_progress(f"Skipping {lang_locale} as unsupported by Android")
            continue

        print_progress(f"Converting translations for {lang_locale} to target format...")

        locale_data = locales.get(lang_locale)
        if locale_data is None:
            raise ValueError(f"Missing locale data for {lang_locale}")

        write_android_xml(
            locale_data['translations'],
            args.translations_output_directory,
            source_locale,
            lang_locale,
            glossary_dict
        )

    print_success("All conversions complete")


if __name__ == "__main__":
    run_main(main)
