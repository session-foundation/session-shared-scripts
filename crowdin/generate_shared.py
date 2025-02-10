import html
import json
import os

def clean_string(text, is_android, glossary_dict, extra_replace_dict):
    to_ret = text
    if(is_android):
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
        text = text.replace("&", "&amp;")   # Assume any remaining ampersands are desired
    else:
        text = html.unescape(text)          # Unescape any HTML escaping

    stripped = to_ret.strip()               # Strip whitespace

    # replace all the defined constants (from crowdin's glossary) in the string
    for glossary_key in glossary_dict:
        stripped = stripped.replace("{" + glossary_key + "}", glossary_dict[glossary_key])

    # if extra_replace_dict has keys, replace those too
    for extra_key in extra_replace_dict:
        stripped = stripped.replace(extra_key, extra_replace_dict[extra_key])
    return stripped


def load_glossary_dict(input_file):
    if not os.path.exists(input_file):
        raise FileNotFoundError(f"Could not find '{input_file}' in raw translations directory")

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
