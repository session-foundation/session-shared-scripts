import os
import json
import xml.etree.ElementTree as ET
import sys
import argparse
import re
from typing import Dict, List, Any, Set, Tuple
from dataclasses import dataclass, field
from colorama import Fore, Style
from generate_shared import setup_generation, load_glossary_dict

XLIFF_NAMESPACE = {'ns': 'urn:oasis:names:tc:xliff:document:1.2'}

# Allowed HTML tags in translations
ALLOWED_TAGS = {'b', 'br', 'span'}

# Regex patterns for validation
VARIABLE_PATTERN = re.compile(r'\{(\w+)\}')
HTML_TAG_PATTERN = re.compile(r'<(/?)(\w+)([^>]*)>')


@dataclass
class ValidationIssue:
    """Represents a single validation issue."""
    locale: str
    string_key: str
    issue_type: str
    message: str
    severity: str = "error"  # "error" or "warning"


@dataclass
class ValidationResult:
    issues: List[ValidationIssue] = field(default_factory=list)

    def add_issue(self, locale: str, string_key: str, issue_type: str, message: str, severity: str = "error"):
        self.issues.append(ValidationIssue(
            locale, string_key, issue_type, message, severity))

    def has_errors(self) -> bool:
        return any(issue.severity == "error" for issue in self.issues)

    def get_error_count(self) -> int:
        return sum(1 for issue in self.issues if issue.severity == "error")

    def get_warning_count(self) -> int:
        return sum(1 for issue in self.issues if issue.severity == "warning")

    def get_issues_by_type(self) -> Dict[str, List[ValidationIssue]]:
        by_type = {}
        for issue in self.issues:
            if issue.issue_type not in by_type:
                by_type[issue.issue_type] = []
            by_type[issue.issue_type].append(issue)
        return by_type

    def get_issues_by_locale(self) -> Dict[str, List[ValidationIssue]]:
        by_locale = {}
        for issue in self.issues:
            if issue.locale not in by_locale:
                by_locale[issue.locale] = []
            by_locale[issue.locale].append(issue)
        return by_locale


def extract_variables(text: str) -> Set[str]:
    """Extract all {variable} placeholders from a string."""
    return set(VARIABLE_PATTERN.findall(text))


def extract_tags(text: str) -> Dict[str, int]:
    """
    Extract HTML tags from a string and count them.
    Returns a dict like {'b': 2, 'br': 1, 'span': 1}
    """
    tag_counts = {}
    for match in HTML_TAG_PATTERN.finditer(text):
        tag_name = match.group(2).lower()
        tag_counts[tag_name] = tag_counts.get(tag_name, 0) + 1
    return tag_counts


def get_string_value(trans_data: Dict[str, Any]) -> str:
    """
    Get the string value from translation data,
    handling both regular strings and plurals.
    """
    if trans_data['type'] == 'plural':
        # For plurals, combine all forms for validation
        return ' '.join(trans_data['forms'].values())
    else:
        return trans_data['value']


def get_all_string_values(trans_data: Dict[str, Any]) -> List[Tuple[str, str]]:
    """
    Get all string values from translation data with their context.
    Returns list of (context, value) tuples.
    """
    if trans_data['type'] == 'plural':
        return [(f"plural.{form}", value) for form, value in trans_data['forms'].items()]
    else:
        return [("value", trans_data['value'])]


def find_disallowed_tags(text: str) -> List[str]:
    """Find any HTML tags that are not in the allowed list."""
    disallowed = []
    for match in HTML_TAG_PATTERN.finditer(text):
        tag_name = match.group(2).lower()
        if tag_name not in ALLOWED_TAGS:
            disallowed.append(match.group(0))
    return disallowed


def find_invalid_angle_brackets(text: str) -> List[str]:
    """
    Find invalid uses of angle brackets that don't form valid tags.
    Only flags '<' that doesn't start a valid tag.
    Standalone '>' is allowed (e.g., "{name} > {conversation_name}").
    """
    issues = []

    i = 0
    while i < len(text):
        if text[i] == '<':
            # Check if this is a valid tag start
            remaining = text[i:]
            # Valid patterns: <b>, </b>, <br/>, <span>, </span>
            valid_tag = re.match(
                r'</?(?:b|br|span)(?:\s*/)?>', remaining, re.IGNORECASE)
            if not valid_tag:
                # Extract context for error message
                snippet = text[i:i+15] + ('...' if len(text) > i+15 else '')
                issues.append(f"Invalid '<' at position {i}: '{snippet}'")
        i += 1

    return issues


def find_invalid_braces(text: str) -> List[str]:
    """
    Find invalid uses of curly braces that don't form valid variables.
    Valid: {variable_name}
    Invalid: {}, {123}, { space }, lone { or }
    """
    issues = []

    i = 0
    while i < len(text):
        if text[i] == '{':
            # Find matching }
            end = text.find('}', i)
            if end == -1:
                issues.append(f"Unmatched '{{' at position {i}")
                i += 1
                continue

            content = text[i+1:end]
            # Check if content is a valid variable name (alphanumeric + underscore)
            if not re.match(r'^\w+$', content):
                snippet = text[i:end+1]
                issues.append(f"Invalid variable syntax '{
                              snippet}' at position {i}")

            i = end + 1
        elif text[i] == '}':
            # Check if there's a matching { before
            before = text[:i]
            last_open = before.rfind('{')
            if last_open == -1:
                issues.append(f"Unmatched '}}' at position {i}")
            i += 1
        else:
            i += 1

    return issues


def validate_all_translations(parsed_locales: Dict[str, Any], source_locale: str) -> ValidationResult:
    """
    Validate all translations against the source locale (English).

    Checks:
    1. All NON-PLURAL strings in all locales have the same variables as English
    2. No locale has extra strings vs English
    3. No invalid curly braces (must be valid {variable} format) - checked for ALL strings
    4. All tags are valid: <br/> <b></b> <span></span> - checked for ALL strings

    Note: Plural strings are only checked for syntax validity (3, 4), NOT for variable/tag
    matching with English, because different languages have different plural forms
    (e.g., English has 2 forms, Arabic has 6, Russian has 4).
    """
    result = ValidationResult()

    if source_locale not in parsed_locales:
        result.add_issue(source_locale, "", "missing_source",
                         f"Source locale '{source_locale}' not found")
        return result

    source_translations = parsed_locales[source_locale]['translations']
    source_keys = set(source_translations.keys())

    # Extract variables and tags from source strings (non-plurals only)
    source_variables = {}
    source_tags = {}
    for key, trans_data in source_translations.items():
        # Only track variables/tags for non-plural strings
        if trans_data['type'] != 'plural':
            text = trans_data['value']
            source_variables[key] = extract_variables(text)
            source_tags[key] = extract_tags(text)

    # Validate each locale
    for locale, locale_data in parsed_locales.items():
        translations = locale_data['translations']
        locale_keys = set(translations.keys())

        # Check 2: No extra keys vs English
        extra_keys = locale_keys - source_keys
        for key in extra_keys:
            result.add_issue(
                locale, key, "extra_key",
                f"String '{key}' exists in {locale} but not in source locale"
            )

        # Validate each string
        for key, trans_data in translations.items():
            # Skip if this is an extra key (already reported)
            if key not in source_keys:
                continue

            is_plural = trans_data['type'] == 'plural'

            # Get all string values (handles plurals)
            string_values = get_all_string_values(trans_data)

            for context, text in string_values:
                full_key = f"{key}" if context == "value" else f"{
                    key} ({context})"

                # Check 1: Variables match source (NON-PLURALS ONLY)
                # Plurals are skipped because different languages have different plural forms
                if not is_plural and key in source_variables:
                    text_variables = extract_variables(text)
                    source_vars = source_variables[key]

                    missing_vars = source_vars - text_variables
                    extra_vars = text_variables - source_vars

                    if missing_vars:
                        result.add_issue(
                            locale, full_key, "missing_variable",
                            f"Missing variables: {{{
                                ', '.join(sorted(missing_vars))}}}"
                        )

                    if extra_vars:
                        result.add_issue(
                            locale, full_key, "extra_variable",
                            f"Extra variables not in source: {{{
                                ', '.join(sorted(extra_vars))}}}"
                        )

                # Check 3: Invalid curly braces (ALL strings including plurals)
                brace_issues = find_invalid_braces(text)
                for issue in brace_issues:
                    result.add_issue(
                        locale, full_key, "invalid_braces",
                        issue
                    )

                # Check 4a: Disallowed tags (ALL strings including plurals)
                disallowed = find_disallowed_tags(text)
                for tag in disallowed:
                    result.add_issue(
                        locale, full_key, "disallowed_tag",
                        f"Disallowed HTML tag: {tag}"
                    )

                # Check 4b: Invalid angle brackets / malformed tags (ALL strings including plurals)
                angle_issues = find_invalid_angle_brackets(text)
                for issue in angle_issues:
                    result.add_issue(
                        locale, full_key, "invalid_tag",
                        issue
                    )

                # Check 4c: Tag count mismatch with source (NON-PLURALS ONLY, non-source locales)
                # Plurals are skipped for the same reason as variables
                if not is_plural and locale != source_locale and key in source_tags:
                    text_tags = extract_tags(text)
                    source_tag_counts = source_tags[key]

                    for tag, source_count in source_tag_counts.items():
                        locale_count = text_tags.get(tag, 0)
                        if locale_count < source_count:
                            result.add_issue(
                                locale, full_key, "missing_tag",
                                f"Missing <{tag}> tags: expected {
                                    source_count}, found {locale_count}",
                                severity="warning"
                            )

    return result


def print_validation_summary(result: ValidationResult):
    if not result.issues:
        print(f"{Fore.GREEN}✅ All validations passed{Style.RESET_ALL}")
        return

    error_count = result.get_error_count()
    warning_count = result.get_warning_count()

    print(f"\n{Fore.YELLOW}{'='*60}{Style.RESET_ALL}")
    print(f"{Fore.YELLOW}Validation Summary{Style.RESET_ALL}")
    print(f"{Fore.YELLOW}{'='*60}{Style.RESET_ALL}")

    by_type = result.get_issues_by_type()
    for issue_type, issues in sorted(by_type.items()):
        error_issues = [i for i in issues if i.severity == "error"]
        warning_issues = [i for i in issues if i.severity == "warning"]

        type_display = issue_type.replace('_', ' ').title()
        if error_issues:
            print(f"{Fore.RED}  {type_display}: {
                  len(error_issues)} errors{Style.RESET_ALL}")
        if warning_issues:
            print(f"{Fore.YELLOW}  {type_display}: {
                  len(warning_issues)} warnings{Style.RESET_ALL}")

    print(f"\n{Fore.YELLOW}Issues by locale:{Style.RESET_ALL}")
    by_locale = result.get_issues_by_locale()
    for locale, issues in sorted(by_locale.items()):
        errors = [i for i in issues if i.severity == "error"]
        warnings = [i for i in issues if i.severity == "warning"]
        if errors or warnings:
            error_str = f"{len(errors)} errors" if errors else ""
            warning_str = f"{len(warnings)} warnings" if warnings else ""
            counts = ", ".join(filter(None, [error_str, warning_str]))
            print(f"  [{locale}] {counts}")

            # Print first few issues for this locale
            for issue in issues[:3]:
                color = Fore.RED if issue.severity == "error" else Fore.YELLOW
                print(
                    f"    {color}- {issue.string_key}: {issue.message}{Style.RESET_ALL}")
            if len(issues) > 3:
                print(f"    ... and {len(issues) - 3} more")

    print(f"\n{Fore.YELLOW}{'='*60}{Style.RESET_ALL}")
    total_str = f"{Fore.RED}{error_count} errors{
        Style.RESET_ALL}" if error_count else ""
    warning_str = f"{Fore.YELLOW}{warning_count} warnings{
        Style.RESET_ALL}" if warning_count else ""
    print(f"Total: {', '.join(filter(None, [total_str, warning_str]))}")
    print(f"{Fore.YELLOW}{'='*60}{Style.RESET_ALL}\n")


def parse_xliff_file(file_path: str, warn_on_missing_target: bool = True) -> Dict[str, Any]:
    """
    Parse a single XLIFF file and return translations as a dictionary.

    Args:
        file_path: Path to the XLIFF file
        warn_on_missing_target: If True, print warnings when target is missing and source is used

    Returns:
        Dictionary with 'translations' and 'target_language' keys
    """
    tree = ET.parse(file_path)
    root = tree.getroot()
    translations = {}

    file_elem = root.find('ns:file', namespaces=XLIFF_NAMESPACE)
    if file_elem is None:
        raise ValueError(f"Invalid XLIFF structure in file: {file_path}")

    target_language = file_elem.get('target-language')
    if target_language is None:
        raise ValueError(f"Missing target-language in file: {file_path}")

    # Handle plural groups first
    for group in root.findall('.//ns:group[@restype="x-gettext-plurals"]', namespaces=XLIFF_NAMESPACE):
        plural_forms = {}
        resname = None

        for trans_unit in group.findall('ns:trans-unit', namespaces=XLIFF_NAMESPACE):
            if resname is None:
                resname = trans_unit.get('resname') or trans_unit.get('id')

            target = trans_unit.find('ns:target', namespaces=XLIFF_NAMESPACE)
            source = trans_unit.find('ns:source', namespaces=XLIFF_NAMESPACE)
            context_group = trans_unit.find(
                'ns:context-group', namespaces=XLIFF_NAMESPACE)

            if context_group is not None:
                plural_form_elem = context_group.find(
                    'ns:context[@context-type="x-plural-form"]',
                    namespaces=XLIFF_NAMESPACE
                )
                if plural_form_elem is not None:
                    form = plural_form_elem.text.split(':')[-1].strip().lower()

                    if target is not None and target.text:
                        plural_forms[form] = target.text
                    elif source is not None and source.text:
                        plural_forms[form] = source.text
                        if warn_on_missing_target:
                            print(f"Warning: Using source text for plural form '{form}' of "
                                  f"'{resname}' in '{target_language}' as target is missing or empty")

        if resname and plural_forms:
            translations[resname] = {
                'type': 'plural',
                'forms': plural_forms
            }

    # Handle non-plural translations (skip entries already processed as plurals)
    for trans_unit in root.findall('.//ns:trans-unit', namespaces=XLIFF_NAMESPACE):
        resname = trans_unit.get('resname') or trans_unit.get('id')
        if resname is None or resname in translations:
            continue

        target = trans_unit.find('ns:target', namespaces=XLIFF_NAMESPACE)
        source = trans_unit.find('ns:source', namespaces=XLIFF_NAMESPACE)

        if target is not None and target.text:
            translations[resname] = {
                'type': 'string',
                'value': target.text
            }
        elif source is not None and source.text:
            translations[resname] = {
                'type': 'string',
                'value': source.text
            }
            if warn_on_missing_target:
                print(f"Warning: Using source text for '{resname}' in "
                      f"'{target_language}' as target is missing or empty")

    return {
        'translations': translations,
        'target_language': target_language
    }


def parse_all_xliff_files(input_directory: str, skip_validation: bool = False) -> Tuple[Dict[str, Any], ValidationResult]:
    """
    Parse all XLIFF files in the input directory and return a combined result.

    Args:
        input_directory: Directory containing XLIFF files and project info
        skip_validation: If True, skip validation step

    Returns:
        Tuple of (parsed data dict, validation result)
    """
    setup_values = setup_generation(input_directory)
    source_language = setup_values['source_language']
    rtl_languages = setup_values['rtl_languages']
    non_translatable_strings_file = setup_values['non_translatable_strings_file']
    target_languages = setup_values['target_languages']

    glossary_dict = load_glossary_dict(non_translatable_strings_file)

    all_languages = [source_language] + target_languages
    parsed_locales = {}

    for language in all_languages:
        lang_locale = language['locale']
        input_file = os.path.join(input_directory, f"{lang_locale}.xliff")

        if not os.path.exists(input_file):
            raise FileNotFoundError(
                f"Could not find '{input_file}' in raw translations directory")

        print(f"\033[2K{Fore.WHITE}⏳ Parsing {
              lang_locale}...{Style.RESET_ALL}", end='\r')

        try:
            result = parse_xliff_file(input_file)
            translations = result['translations']
            target_language = result['target_language']

            parsed_locales[lang_locale] = {
                'target_language': target_language,
                'translations': translations,
                'language_info': language
            }

        except Exception as e:
            raise ValueError(f"Error processing locale {
                             lang_locale}: {str(e)}")

    print(f"\033[2K{Fore.GREEN}✅ Parsed {
          len(parsed_locales)} locale files{Style.RESET_ALL}")

    # Run validation
    validation_result = ValidationResult()
    if not skip_validation:
        print(f"{Fore.WHITE}⏳ Validating translations...{
              Style.RESET_ALL}", end='\r')
        source_locale = source_language['locale']
        validation_result = validate_all_translations(
            parsed_locales, source_locale)

        if validation_result.issues:
            print(f"\033[2K{Fore.YELLOW}⚠️  Validation completed with issues{
                  Style.RESET_ALL}")
        else:
            print(f"\033[2K{Fore.GREEN}✅ Validation passed{Style.RESET_ALL}")

    parsed_data = {
        'source_language': source_language,
        'target_languages': target_languages,
        'rtl_languages': rtl_languages,
        'glossary': glossary_dict,
        'locales': parsed_locales
    }

    return parsed_data, validation_result


def main():
    parser = argparse.ArgumentParser(
        description='Parse XLIFF translation files into an intermediate JSON format'
    )
    parser.add_argument(
        'raw_translations_directory',
        help='Directory which contains the raw translation files'
    )
    parser.add_argument(
        'output_file',
        help='Path to save the parsed translations JSON file'
    )
    parser.add_argument(
        '--skip-validation',
        action='store_true',
        help='Skip validation of translations'
    )
    parser.add_argument(
        '--error-on-validation-failure',
        action='store_true',
        help='Exit with error code if validation fails'
    )
    parser.add_argument(
        '--validation-report',
        help='Path to save validation report JSON file'
    )
    args = parser.parse_args()

    try:
        parsed_data, validation_result = parse_all_xliff_files(
            args.raw_translations_directory,
            skip_validation=args.skip_validation
        )

        if not args.skip_validation:
            print_validation_summary(validation_result)

        if args.validation_report and validation_result.issues:
            report = {
                'error_count': validation_result.get_error_count(),
                'warning_count': validation_result.get_warning_count(),
                'issues': [
                    {
                        'locale': issue.locale,
                        'string_key': issue.string_key,
                        'issue_type': issue.issue_type,
                        'message': issue.message,
                        'severity': issue.severity
                    }
                    for issue in validation_result.issues
                ]
            }
            os.makedirs(os.path.dirname(args.validation_report)
                        or '.', exist_ok=True)
            with open(args.validation_report, 'w', encoding='utf-8') as f:
                json.dump(report, f, ensure_ascii=False, indent=2)
            print(f"{Fore.WHITE}Validation report saved to {
                  args.validation_report}{Style.RESET_ALL}")

        os.makedirs(os.path.dirname(args.output_file) or '.', exist_ok=True)
        with open(args.output_file, 'w', encoding='utf-8') as f:
            json.dump(parsed_data, f, ensure_ascii=False, indent=2)

        print(f"{Fore.GREEN}✅ Parsed translations saved to {
              args.output_file}{Style.RESET_ALL}")

        if args.error_on_validation_failure and validation_result.has_errors():
            print(f"{Fore.RED}❌ Exiting with error due to validation failures{
                  Style.RESET_ALL}")
            sys.exit(1)

    except KeyboardInterrupt:
        print("\nProcess interrupted by user")
        sys.exit(0)
    except Exception as e:
        print(f"\033[2K{Fore.RED}❌ An error occurred: {
              str(e)}{Style.RESET_ALL}")
        sys.exit(1)


if __name__ == "__main__":
    main()
