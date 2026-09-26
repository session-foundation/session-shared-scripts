# Crowdin Duplicate Translations

Posts the Crowdin string slots that hold more than one translation, so someone can keep
one and delete the rest: exactly one translation per string, and per plural category
for a plural string, is what gets exported. A slot is (string, locale, plural category).

| | |
| --- | --- |
| Runs | `crowdin-relay.service`, always on, behind nginx at `POST /crowdin/suggestions/<secret>`; `session-ops@crowdin-duplicates.timer`, daily 03:00 UTC |
| Secrets | `/etc/session-ops/crowdin.env`: a read-only `CROWDIN_API_TOKEN`, the channel's webhook, `CROWDIN_WEBHOOK_SECRET` |
| Dry run | `session-ops run crowdin-duplicates --dry-run -- --locales de`; `CROWDIN_RELAY_DRY_RUN=1` for the relay |
| Re-run | `systemctl start session-ops@crowdin-duplicates.service` |
| Logs | `journalctl -u crowdin-relay -u session-ops@crowdin-duplicates -n 50 --no-pager` |

## How it stays current

The open slots live in `/var/lib/session-ops/crowdin-duplicates/duplicates.json`. Each run posts
only what changed: slots newly holding 2+ translations, and slots that no longer do.
Nothing changed, nothing is posted.

- **The relay** takes Crowdin's suggestion events (added, updated, deleted, approved,
  disapproved), answers at once, then re-checks the one (string, locale) the event names.
  Crowdin signs nothing, so the secret is the webhook URL's last path segment.
- **Reconciliation** judges every string of every locale once a day. Crowdin never
  retries a webhook it failed to deliver, so this is what keeps the state correct; the
  relay only makes it prompt. Anything missed is posted at most a day late. A slot
  whose string was deleted, or whose locale left the project, resolves here: the
  relay cannot see either.

Both write the state under a file lock. The relay records when it checked each
(string, locale), and reconciliation ignores its own older view of that one, so a scan
that started before a suggestion landed cannot resolve what the event just opened.
The state is written only once Discord accepted every message, so a failed post is
repeated in full rather than lost.

Losing the state is not harmless the way a digest's dedup file is: every open slot
would be reported again. So reconciliation, like the relay, refuses to run without a
state file; seed one with `--seed`, which records without posting.

## Cost

The timer runs with `--croql`: a locale is one query for the strings CroQL counts two or
more translations for, then one request per candidate, about 50 of the project's 1,371
strings. A plural string with a single translation per category is a candidate too,
since CroQL cannot tell categories apart. Checked on 2026-09-25 against a full scan:
a planted second suggestion on a singular string in `fr` was the one singular candidate
across all 80 locales, and both found the same slot.

Without `--croql`, a locale is one request per string: about 110,000 requests for the
whole project, an hour at the 30 requests a second the client allows itself.
`session-ops run crowdin-duplicates -- --locales fr` without it is the way to check
the narrowing again.

## The sharded report

`crowdin-report-duplicates` is the scan this replaces: it lists every open slot rather
than what changed, a rotating eighth of the locales a day, from
`.github/workflows/crowdin_multiple_translations_report.yml`. It stays until a full
reconciliation cycle has run clean on the host. `--json` writes the complete findings,
which is still the way to get every open slot in one file.

## Approving by hand

`crowdin-approve-strings` approves the newest translation of named strings in every
locale, filtered by submitter. It is the one script here that writes to Crowdin, so it
reads a token of its own, from the keyring or `CROWDIN_PROOFREADER_TOKEN`:

```sh
secret-tool store --label='Crowdin proofreader token' service crowdin key proofreader-api-token
uv run crowdin-approve-strings --list ongoingAppeal
uv run crowdin-approve-strings --dry-run --by-user alice ongoingAppeal
```

Every other Crowdin job reads `CROWDIN_API_TOKEN` (keyring `translation-api-token`),
which needs read access only.
