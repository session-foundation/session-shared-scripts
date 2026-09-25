"""
Weekly: download the approved translations from Crowdin, validate them, generate each
platform's strings, and publish them.

- session-android and session-ios get a pull request from
  feature/update-crowdin-translations, rebuilt from `dev` each run.
- session-localization gets a commit straight onto `main`.

Each platform is checked out shallow and sparse, only the paths its generator writes.
The three are independent targets: one failing to publish does not stop the others,
and the alert says which. Android's own CI validates its pull request, so no Gradle
build runs here.

The run's inputs, outputs and validation report are kept under the job's
runs/<stamp>/ for 14 days, which is what the workflow's artefacts were for.

Config (env vars):
    CROWDIN_API_TOKEN     read-only
    PUBLISH_GIT_AUTHOR    "Name <email>" the commits are authored as
    plus what shared/github.py reads to publish (not needed with --dry-run)

    session-ops run crowdin-sync [--dry-run] [-- --only android]
"""
import argparse
import os
import shutil
import tempfile
import time

from session_ops.crowdin import (codegen_localization, download_translations_from_crowdin,
                                 generate_android_strings, generate_ios_strings,
                                 generate_language_list, parse_xliff)
from session_ops.ops.runner import Outcome, call_target, step
from session_ops.platforms import publish
from session_ops.shared import github
from session_ops.shared.git import Repo

PROJECT_ID = "618696"
GLOSSARY_ID, CONCEPT_ID = "407522", "36"
BOT_BRANCH = "feature/update-crowdin-translations"
TITLE = "[Automated] Update translations from Crowdin"
BODY = """[Automated]
This PR includes the latest translations from Crowdin

Session uses the community-driven translation platform Crowdin for localization, anyone can contribute at https://getsession.org/translate
"""

ANDROID_RES = "app/src/main/res"
ANDROID_CONSTANTS = "app/src/main/java/org/session/libsession/utilities/NonTranslatableStringConstants.kt"
IOS_TRANSLATIONS = "Session/Meta/Translations"
IOS_CONSTANTS = "SessionUIKit/Style Guide/Constants.swift"

TARGETS = ("android", "ios", "localization")
REPOS = {"android": "session-android", "ios": "session-ios",
         "localization": "session-localization"}


def checkout(target, work, token):
    patterns = {
        "android": [f"/{ANDROID_RES}/values*/strings.xml", f"/{ANDROID_CONSTANTS}"],
        "ios": [f"/{IOS_TRANSLATIONS}/", f"/{IOS_CONSTANTS}"],
        "localization": ["/generated/"],
    }[target]
    branch = "main" if target == "localization" else "dev"
    url = f"{publish.GITHUB}/{publish.ORG}/{REPOS[target]}"
    return Repo.sparse_clone(url, branch, os.path.join(work, target), patterns, token)


def generate(target, repo, parsed):
    root = repo.path
    if target == "android":
        # Stale locales drop out: the generator writes only the ones Crowdin has.
        res = os.path.join(root, ANDROID_RES)
        for entry in os.listdir(res) if os.path.isdir(res) else []:
            strings = os.path.join(res, entry, "strings.xml")
            if entry.startswith("values") and os.path.exists(strings):
                os.remove(strings)
        generate_android_strings.main([parsed, res, os.path.join(root, ANDROID_CONSTANTS)])
    elif target == "ios":
        generate_ios_strings.main([parsed, os.path.join(root, IOS_TRANSLATIONS),
                                   os.path.join(root, IOS_CONSTANTS)])
    else:
        generated = os.path.join(root, "generated")
        codegen_localization.main([parsed, generated])
        generate_language_list.main([parsed, generated])


def publish_target(target, repo, api, author, dry_run):
    name = f"{publish.ORG}/{REPOS[target]}"
    if target == "localization":
        return publish.direct_push(repo, name, "main", TITLE, author, dry_run)
    return publish.pull_request(repo, api, name, "dev", BOT_BRANCH, TITLE, BODY, author,
                                dry_run)


def sync_target(target, work, parsed, token, api, author, dry_run):
    step(f"{target}: checkout")
    repo = checkout(target, work, token)
    step(f"{target}: generate")
    generate(target, repo, parsed)
    step(f"{target}: publish")
    print(publish_target(target, repo, api, author, dry_run))


def keep(work, runs_dir, names):
    """Copy the run's own files where they outlive it."""
    if not runs_dir:
        return
    dest = os.path.join(runs_dir, time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()))
    os.makedirs(dest, exist_ok=True)
    for name in names:
        source = os.path.join(work, name)
        if os.path.isdir(source):
            shutil.copytree(source, os.path.join(dest, name))
        elif os.path.exists(source):
            shutil.copy2(source, dest)
    print(f"Kept this run in {dest}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.strip().split("\n")[0])
    parser.add_argument("--only", nargs="+", choices=TARGETS, default=list(TARGETS))
    parser.add_argument("--skip-validation-errors", action="store_true",
                        help="Publish even when some translations fail validation.")
    parser.add_argument("--runs", metavar="DIR", help="Where to keep a copy of the run.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Generate and commit locally; push nothing, open nothing.")
    args = parser.parse_args(argv)

    author = os.environ.get("PUBLISH_GIT_AUTHOR") or "session-ops <session-ops@localhost>"
    work = os.environ.get("SESSION_OPS_WORK_DIR") or tempfile.mkdtemp(prefix="crowdin-sync-")
    raw, parsed = os.path.join(work, "raw"), os.path.join(work, "parsed_translations.json")
    report = os.path.join(work, "validation_report.json")

    step("download")
    download_translations_from_crowdin.main([
        os.environ["CROWDIN_API_TOKEN"], PROJECT_ID, raw, "--glossary_id", GLOSSARY_ID,
        "--concept_id", CONCEPT_ID, "--skip-untranslated-strings"])
    step("parse and validate")
    try:
        parse_xliff.main([raw, parsed, "--validation-report", report,
                          *([] if args.skip_validation_errors
                            else ["--error-on-validation-failure"])])
    finally:
        keep(work, args.runs, ["raw", "parsed_translations.json", "validation_report.json"])

    token = None if args.dry_run else github.publish_token(
        publish.ORG, [REPOS[t] for t in args.only])
    api = github.session(token) if token else None
    results = {target: call_target(lambda _, target=target: sync_target(
                   target, work, parsed, token, api, author, args.dry_run), [])
               for target in args.only}
    return Outcome(targets=results)
