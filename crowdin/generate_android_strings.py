import os
import xml.etree.ElementTree as ET
import sys
import argparse
import re
from pathlib import Path
from colorama import Fore, Style
from generate_shared import load_glossary_dict, clean_string, setup_generation

# Variables that should be treated as numeric (using %d)
NUMERIC_VARIABLES = ['count', 'found_count', 'total_count']


# Parse command-line arguments
parser = argparse.ArgumentParser(description='Convert a XLIFF translation files to Android XML.')
parser.add_argument('raw_translations_directory', help='Directory which contains the raw translation files')
parser.add_argument('translations_output_directory', help='Directory to save the converted translation files')
parser.add_argument('non_translatable_strings_output_path', help='Path to save the non-translatable strings to')
args = parser.parse_args()

INPUT_DIRECTORY = args.raw_translations_directory
TRANSLATIONS_OUTPUT_DIRECTORY = args.translations_output_directory
NON_TRANSLATABLE_STRINGS_OUTPUT_PATH = args.non_translatable_strings_output_path

def parse_xliff(file_path):
    tree = ET.parse(file_path)
    root = tree.getroot()
    namespace = {'ns': 'urn:oasis:names:tc:xliff:document:1.2'}
    translations = {}

    # Handle plural groups
    for group in root.findall('.//ns:group[@restype="x-gettext-plurals"]', namespaces=namespace):
        plural_forms = {}
        resname = None
        for trans_unit in group.findall('ns:trans-unit', namespaces=namespace):
            if resname is None:
                resname = trans_unit.get('resname')
            target = trans_unit.find('ns:target', namespaces=namespace)
            context_group = trans_unit.find('ns:context-group', namespaces=namespace)
            plural_form = context_group.find('ns:context[@context-type="x-plural-form"]', namespaces=namespace)
            if target is not None and target.text and plural_form is not None:
                form = plural_form.text.split(':')[-1].strip().lower()
                plural_forms[form] = target.text
        if resname and plural_forms:
            translations[resname] = plural_forms

    # Handle non-plural translations
    for trans_unit in root.findall('.//ns:trans-unit', namespaces=namespace):
        resname = trans_unit.get('resname')
        if resname not in translations:  # This is not part of a plural group
            target = trans_unit.find('ns:target', namespaces=namespace)
            if target is not None and target.text:
                translations[resname] = target.text

    return translations

def convert_placeholders(text):
    def repl(match):
        var_name = match.group(1)
        index = len(set(re.findall(r'\{([^}]+)\}', text[:match.start()]))) + 1

        if var_name in NUMERIC_VARIABLES:
            return f"%{index}$d"
        else:
            return f"%{index}$s"

    return re.sub(r'\{([^}]+)\}', repl, text)


def generate_android_xml(translations, app_name, glossary_dict):
    sorted_translations = sorted(translations.items())
    result = '<?xml version="1.0" encoding="utf-8"?>\n'
    result += '<resources>\n'

    if app_name is not None:
        result += f'    <string name="app_name" translatable="false">{app_name}</string>\n'

    for resname, target in sorted_translations:
        if isinstance(target, dict):  # It's a plural group
            result += f'    <plurals name="{resname}">\n'
            for form, value in target.items():
                escaped_value = clean_string(convert_placeholders(value), True, glossary_dict, {})
                result += f'        <item quantity="{form}">{escaped_value}</item>\n'
            result += '    </plurals>\n'
        else:  # It's a regular string (for these we DON'T want to convert the placeholders)
            escaped_target = clean_string(target, True, glossary_dict, {})
            result += f'    <string name="{resname}">{escaped_target}</string>\n'

    result += '</resources>'

    return result

def convert_xliff_to_android_xml(input_file, output_dir, source_locale, locale, glossary_dict):
    if not os.path.exists(input_file):
        raise FileNotFoundError(f"Could not find '{input_file}' in raw translations directory")

    # Parse the XLIFF and convert to XML (only include the 'app_name' entry in the source language)
    is_source_language = locale == source_locale
    translations = parse_xliff(input_file)
    app_name = glossary_dict['app_name']
    output_data = generate_android_xml(translations, app_name if is_source_language else None, glossary_dict)

    # android is pretty smart to resolve resources for translations, see the example here:
    # https://developer.android.com/guide/topics/resources/multilingual-support#resource-resolution-examples
    android_safe_locale = f"b+{locale.replace('-','+')}"

    # Generate output files
    if is_source_language:
        language_output_dir = os.path.join(output_dir, 'values')
    else:
        language_output_dir = os.path.join(output_dir, f'values-{android_safe_locale}')

    os.makedirs(language_output_dir, exist_ok=True)
    language_output_file = os.path.join(language_output_dir, 'strings.xml')
    with open(language_output_file, 'w', encoding='utf-8') as file:
        file.write(output_data)



def convert_non_translatable_strings_to_kotlin(input_file, output_path):
    glossary_dict = load_glossary_dict(input_file)

    max_key_length = max(len(key) for key in glossary_dict)
    app_name = glossary_dict['app_name']

    # Output the file in the desired format
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    with open(output_path, 'w', encoding='utf-8') as file:
        file.write('package org.session.libsession.utilities\n')
        file.write('\n')
        file.write('// Non-translatable strings for use with the UI\n')
        file.write("object NonTranslatableStringConstants {\n")
        for key_lowercase in glossary_dict:
            key = key_lowercase.upper()
            text = glossary_dict[key_lowercase]
            file.write(f'    const val {key:<{max_key_length}} = "{text}"\n')

        file.write('}\n')
        file.write('\n')

    if not app_name:
        raise ValueError("could not find app_name in glossary_dict")

def convert_all_files(input_directory: str ):
    setup_values = setup_generation(input_directory)
    source_language, rtl_languages, non_translatable_strings_file, target_languages = setup_values.values()

    convert_non_translatable_strings_to_kotlin(non_translatable_strings_file, NON_TRANSLATABLE_STRINGS_OUTPUT_PATH)
    print(f"\033[2K{Fore.GREEN}✅ Static string generation complete{Style.RESET_ALL}")
    glossary_dict = load_glossary_dict(non_translatable_strings_file)

    # Convert the XLIFF data to the desired format
    print(f"\033[2K{Fore.WHITE}⏳ Converting translations to target format...{Style.RESET_ALL}", end='\r')
    source_locale = source_language['locale']
    for language in [source_language] + target_languages:
        lang_locale = language['locale']
        if lang_locale == 'sh-HR':
            # see https://en.wikipedia.org/wiki/Language_secessionism#In_Serbo-Croatian
            print(f"\033[2K{Fore.WHITE}⏳ Skipping {lang_locale} as unsupported by android{Style.RESET_ALL}")
            continue
        print(f"\033[2K{Fore.WHITE}⏳ Converting translations for {lang_locale} to target format...{Style.RESET_ALL}", end='\r')
        input_file = os.path.join(input_directory, f"{lang_locale}.xliff")
        convert_xliff_to_android_xml(input_file, TRANSLATIONS_OUTPUT_DIRECTORY, source_locale, lang_locale, glossary_dict)
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
