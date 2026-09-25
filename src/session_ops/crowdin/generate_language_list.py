#!/usr/bin/env python3
"""
Language List Generator

Generates `languages.ts` for the Typescript Localization Module: every locale's name written in
its own language, its name in English where that is a different word, and the country CLDR
associates with it so a picker can put a flag beside it.

Generated here rather than asked of the runtime because not every runtime can answer: React
Native's Hermes ships neither `Intl.DisplayNames` nor `Intl.Locale`.
"""

import argparse
import os
from collections import Counter
from typing import Any, Dict, List, Optional

from babel import Locale
from babel.core import get_global
from session_ops.crowdin.generate_shared import (
    DISCLAIMER_GENERATED,
    cldr_code,
    cldr_locale,
    get_locale_key,
    load_parsed_translations,
    print_progress,
    print_success,
    print_warning,
    run_main
)

ENGLISH = Locale('en')


def locale_keys(parsed_data: Dict[str, Any]) -> List[str]:
    """The locale keys the module is generated with, which is what `crowdinLocales` holds."""
    languages = [parsed_data['source_language']] + parsed_data['target_languages']
    return sorted({
        get_locale_key(lang['locale'], lang['twoLettersCode']) for lang in languages
    })


def display_name(locale_key: str, locale: Optional[Locale], in_locale: Locale) -> Optional[str]:
    """
    CLDR's name for a locale code, preferring the name it gives that exact code.

    The exact code is what keeps apart the pairs CLDR treats as aliases of one another: `tl` is
    Tagalog where `fil` is Filipino, `sh` is Serbo-Croatian where `sr` is Serbian. Naming them
    from the resolved locale instead gives both members of a pair the same name.
    """
    exact = in_locale.languages.get(cldr_code(locale_key).replace('-', '_'))
    if exact:
        return exact
    return locale.get_display_name(in_locale) if locale is not None else None


def is_country(territory: str) -> bool:
    """CLDR counts `001` (the world) and `419` (Latin America) as territories; no flag has those."""
    return len(territory) == 2 and territory.isalpha() and bool(ENGLISH.territories.get(territory))


def territory_for(locale: Optional[Locale]) -> Optional[str]:
    """
    The country CLDR associates with a locale, or None where there is no country to name.

    A language has no country, so the closest thing to one is the country of its most likely
    form. A locale that names a region which is not a country keeps that answer rather than
    falling back to its language's country: Spain's flag on `es-419` would be wrong rather than
    missing.
    """
    if locale is None:
        return None
    if locale.territory:
        return locale.territory if is_country(locale.territory) else None

    likely = get_global('likely_subtags')
    maximized = likely.get(str(locale)) or likely.get(locale.language)
    territory = maximized.split('_')[-1] if maximized else None
    return territory if territory and is_country(territory) else None


def ts_string(value: str) -> str:
    return "'" + value.replace('\\', '\\\\').replace("'", "\\'") + "'"


def ts_record(entries: Dict[str, Optional[str]]) -> str:
    lines = [
        f"  '{key}': {ts_string(value) if value is not None else 'null'},"
        for key, value in sorted(entries.items())
    ]
    return "{\n" + "\n".join(lines) + "\n}"


def build_language_data(keys: List[str]):
    """Resolve every locale key against CLDR, returning its name, English name and country."""
    names: Dict[str, Optional[str]] = {}
    english: Dict[str, Optional[str]] = {}
    territories: Dict[str, Optional[str]] = {}
    unnamed: List[str] = []

    for key in keys:
        locale = cldr_locale(key)
        own_name = display_name(key, locale, locale) if locale is not None else None
        english_name = display_name(key, locale, ENGLISH)

        if not own_name:
            unnamed.append(key)
        names[key] = own_name or english_name or key
        if english_name and english_name != names[key]:
            english[key] = english_name
        territories[key] = territory_for(locale)

    return names, english, territories, unnamed


def generate_languages_ts(parsed_data: Dict[str, Any], output_path: str):
    keys = locale_keys(parsed_data)
    names, english, territories, unnamed = build_language_data(keys)

    # Reported rather than raised: this runs in the weekly translation job, and a language nobody
    # has named yet must not hold up everyone else's strings. The fix is an override in
    # languageList.ts, which is a decision somebody makes rather than one this script can.
    if unnamed:
        print_warning(f"No CLDR name for {', '.join(unnamed)}, naming them in English or by code")
    shared = sorted(name for name, count in Counter(names.values()).items() if count > 1)
    if shared:
        print_warning(f"Named the same in CLDR: {', '.join(shared)}; override them to tell apart")

    content = f"""{DISCLAIMER_GENERATED}import type {{ CrowdinLocale }} from './constants';

/** Each locale's name in its own language, so a picker reads "Français" rather than "fr". */
export const languageNames: Record<CrowdinLocale, string> = {ts_record(names)};

/**
 * Each locale's name in English, where that is a different word from its own name.
 *
 * Partial by design: a row whose English name is the same word has nothing to add here.
 */
export const languageNamesInEnglish: Partial<Record<CrowdinLocale, string>> = {ts_record(english)};

/**
 * The country CLDR associates with each locale, or null where there is no country to name.
 *
 * Inferred for most of them, so it says where a language is mostly spoken rather than which
 * country it belongs to.
 */
export const languageTerritories: Record<CrowdinLocale, string | null> = {ts_record(territories)};
"""

    os.makedirs(os.path.dirname(output_path) or '.', exist_ok=True)
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(content)

    named_countries = len([t for t in territories.values() if t is not None])
    print_success(
        f"Generated {output_path}: {len(keys)} locales, "
        f"{named_countries} with a country, {len(keys) - named_countries} without"
    )


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='Generate the localization module language list from parsed translations'
    )
    parser.add_argument(
        'parsed_translations_file',
        help='Path to the parsed translations JSON file'
    )
    parser.add_argument(
        'output_directory',
        help='Directory to write the output file (languages.ts)'
    )
    args = parser.parse_args(argv)

    print_progress("Loading parsed translations...")
    parsed_data = load_parsed_translations(args.parsed_translations_file)

    print_progress("Generating languages.ts...")
    generate_languages_ts(parsed_data, os.path.join(args.output_directory, 'languages.ts'))


def cli():
    run_main(main)


if __name__ == "__main__":
    cli()
