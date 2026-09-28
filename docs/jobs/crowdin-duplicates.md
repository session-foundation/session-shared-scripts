# Crowdin Duplicate Translations

Posts the Crowdin string slots that hold more than one translation, so someone can keep
one and delete the rest: exactly one translation per string, and per plural category
for a plural string, is what gets exported. A slot is (string, locale, plural category).

| | |
| --- | --- |
| Runs | `session-ops@crowdin-duplicates.timer`, daily 03:00 UTC |
| Secrets | `/etc/session-ops/crowdin.env`: a read-only `CROWDIN_API_TOKEN` and the channel's webhook |
| Dry run | `session-ops run crowdin-duplicates --dry-run -- --locales de` |
| Re-run | `systemctl start session-ops@crowdin-duplicates.service` |
| Logs | `journalctl -u session-ops@crowdin-duplicates -n 50 --no-pager` |

## How it stays current

The open slots live in `/var/lib/session-ops/crowdin-duplicates/duplicates.json`. Each run posts
only what changed: slots newly holding 2+ translations, and slots that no longer do.
Nothing changed, nothing is posted.

Reconciliation judges every string of every locale once a day, so a new duplicate is
posted within a day, well before the weekly export. A slot whose string was deleted,
or whose locale left the project, resolves. One reconciliation runs at a time: one
started while another holds the state's lock exits. The state is written only once
Discord accepted every message, so a failed post is repeated in full rather than lost.

Losing the state is not harmless the way a digest's dedup file is: every open slot
would be reported again. So reconciliation refuses to run without a state file; seed
one with `--seed`, which records without posting.

## Cost

The timer runs with `--croql`: a locale is one query for the strings CroQL counts two or
more translations for, then one request per candidate, about 50 of the project's 1,371
strings. A plural string with a single translation per category is a candidate too,
since CroQL cannot tell categories apart. Checked on 2026-09-25 against a full scan:
a planted second suggestion on a singular string in `fr` was the one singular candidate
across all 80 locales, and both found the same slot.

A full run takes about 9 minutes, most of it the two listings each locale costs before
its candidates. Without `--croql`, a locale is one request per string: about a minute
each, and 85 minutes for the whole project.
`session-ops run crowdin-duplicates -- --locales fr` without it is the way to check
the narrowing again.

## The sharded report

`crowdin-report-duplicates` is the scan this replaces: it lists every open slot rather
than what changed, a rotating eighth of the locales a day, from
`.github/workflows/crowdin_multiple_translations_report.yml`. It stays until a full
reconciliation cycle has run clean on the host. `--json` writes the complete findings,
which is still the way to get every open slot in one file.
