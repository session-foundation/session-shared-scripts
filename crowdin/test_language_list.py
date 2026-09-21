#!/usr/bin/env python3
"""Tests for the language list generator.

Stdlib unittest so the repo needs no test dependency. Run from anywhere:

    python -m unittest discover -s crowdin -v

Offline: CLDR ships with Babel, so nothing here talks to Crowdin.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from babel import Locale  # noqa: E402  (needs the path insert above)
from generate_shared import cldr_locale  # noqa: E402
from generate_language_list import (  # noqa: E402
    build_language_data,
    display_name,
    locale_keys,
    territory_for,
    ts_record
)

ENGLISH = Locale('en')


def language(locale: str, text_direction: str = 'ltr', two_letters: str = None):
    return {
        'locale': locale,
        'twoLettersCode': two_letters or locale,
        'textDirection': text_direction
    }


def named(locale_key: str):
    locale = cldr_locale(locale_key)
    return display_name(locale_key, locale, locale), display_name(locale_key, locale, ENGLISH)


class TestLocaleKeys(unittest.TestCase):
    def test_keys_match_the_modules_locale_codes(self):
        parsed = {
            'source_language': language('en-US', two_letters='en'),
            'target_languages': [language('kmr-TR', two_letters='ku'), language('pt-BR')]
        }
        self.assertEqual(locale_keys(parsed), ['en', 'kmr', 'pt-BR'])


class TestNames(unittest.TestCase):
    def test_a_code_is_named_as_itself_not_as_what_it_aliases(self):
        # CLDR files `tl` under `fil` and `sh` under `sr`, and names each pair identically unless
        # the exact code is asked for.
        self.assertEqual(named('tl')[0], 'Tagalog')
        self.assertEqual(named('fil')[0], 'Filipino')
        self.assertEqual(named('sh')[1], 'Serbo-Croatian')
        self.assertEqual(named('sr-CS')[1], 'Serbian (Latin)')

    def test_a_catalogue_is_named_in_the_language_it_is_written_in(self):
        self.assertEqual(named('ku')[1], 'Central Kurdish')
        self.assertEqual(named('kmr')[1], 'Kurdish')
        self.assertEqual(named('sr-SP')[1], 'Serbian (Cyrillic)')

    def test_english_name_is_dropped_where_it_is_the_name_already(self):
        _, english, _, _ = build_language_data(['en', 'de'])
        self.assertNotIn('en', english)
        self.assertEqual(english['de'], 'German')

    def test_every_locale_is_named_and_placed(self):
        keys = ['af', 'bal', 'eo', 'es-419', 'sh', 'sr-SP', 'zh-CN']
        names, _, territories, unnamed = build_language_data(keys)
        self.assertEqual(unnamed, [])
        self.assertEqual(sorted(names), sorted(keys))
        self.assertEqual(sorted(territories), sorted(keys))


class TestTerritories(unittest.TestCase):
    def test_a_language_takes_the_country_of_its_likeliest_form(self):
        self.assertEqual(territory_for(cldr_locale('fr')), 'FR')
        self.assertEqual(territory_for(cldr_locale('bal')), 'PK')
        self.assertEqual(territory_for(cldr_locale('ku')), 'IQ')

    def test_a_region_that_is_no_country_gets_no_flag(self):
        # Latin America and the world are territories to CLDR; Spain's flag on `es-419` would be a
        # wrong answer rather than a missing one.
        self.assertIsNone(territory_for(cldr_locale('es-419')))
        self.assertIsNone(territory_for(cldr_locale('eo')))


class TestOutput(unittest.TestCase):
    def test_records_quote_keys_and_escape_values(self):
        self.assertEqual(
            ts_record({'zh-CN': "a'b", 'eo': None}),
            "{\n  'eo': null,\n  'zh-CN': 'a\\'b',\n}"
        )


if __name__ == '__main__':
    unittest.main()
