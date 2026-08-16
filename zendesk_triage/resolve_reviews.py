#!/usr/bin/env python3
"""
Solve the app-store reviews that were never going to be actioned.

59% of all tickets are 4-5★ AppFollow reviews with nothing to act on, and 4,812 of
them sit unsolved in `new`. The daily triage already counts them without spending
tokens (see partition_reviews in triage.py); this closes them out so the unsolved
backlog reflects work that actually exists.

    ⚠️  This writes to Zendesk. Nothing happens without --apply: by default the
        script reports exactly what it would solve and exits.

    ⚠️  Solving a ticket can fire triggers and automations, including satisfaction
        surveys, and an AppFollow requester may carry a real email address. Check
        Admin Center → Objects and rules → Business rules before the first --apply,
        and do that run with a small --max-tickets so the effects are observable.
        Every ticket is tagged (--tag) so a trigger can exclude them.

What it will and will not touch, deliberately narrow:

  * app-store reviews only, by the same detection the triage uses — the Zendesk
    `via.channel`, or a leading ★ run in the subject
  * with a parsed rating of MIN_STARS (4) or better — a fixed floor, not an option,
    because 3★ and below are what the triage wants to see. A review whose stars
    cannot be parsed is skipped, never solved
  * in `new` only. The other 441 unsolved reviews are `open`, and every one of a
    100-ticket sample had an assignee, a group, and an updated_at past its
    created_at — something already acted on them, which is exactly what a bulk
    status change should keep its hands off
  * `solved`, never `closed` — solved is reversible, closed is not

No state file: solved tickets drop out of the query, so runs are idempotent and a
weekly schedule drains the backlog and then keeps pace with new reviews.

An applied run reports what it did to the same Discord channel as the daily triage
("Marked 12 4★ and 31 5★ app-store reviews as solved"), so a job that quietly bulk-
edits tickets is visible where the tickets are already discussed. A run that solves
nothing posts nothing.

Config (env vars, or flags for local runs):
    ZENDESK_SUBDOMAIN     e.g. "mycompany"  -> https://mycompany.zendesk.com
    ZENDESK_EMAIL         agent email for API token auth
    ZENDESK_API_TOKEN     Zendesk API token
    DISCORD_WEBHOOK_URL   Discord incoming webhook (only needed with --apply)

Usage:
    # report what would be solved, touch nothing (the default)
    python resolve_reviews.py

    # actually solve them
    python resolve_reviews.py --apply

    # first real run: small, observable
    python resolve_reviews.py --apply --max-tickets 5

    # solve them without telling the channel
    python resolve_reviews.py --apply --no-discord
"""
import argparse
import os
import sys
import time

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import triage  # noqa: E402  (needs the path insert above)

# Reviews at or above this rating carry nothing to act on. Fixed rather than a flag:
# 3★ and below are what the triage treats as bug reports in disguise, so lowering the
# floor would have this job close the reviews most worth reading. Changing it is a
# deliberate edit here, not something a dispatch can do by accident.
MIN_STARS = 4
# Zendesk's search API returns at most 1000 results, so a run can never see more
# than that anyway. At ~420 new reviews a week the first few runs drain the
# backlog and every run after that clears the week's intake.
DEFAULT_MAX_TICKETS = 1000
# update_many takes at most 100 ids per request.
# https://developer.zendesk.com/api-reference/ticketing/tickets/tickets/#update-many-tickets
BATCH_SIZE = 100
RESOLVED_TAG = "auto-resolved-review"
# Long enough for a 100-ticket batch, short enough that a wedged job fails the run
# rather than holding a scheduled job open.
JOB_TIMEOUT_SECONDS = 300
JOB_POLL_SECONDS = 3


def build_query():
    """Untouched app-store reviews, newest first.

    `via:any_channel` is what AppFollow imports arrive on, and it is the cheap half
    of the filter — the star rating lives in the subject, which Zendesk's search
    index will not match, so the rating is applied locally in select_resolvable.

    `status:new` rather than `status<solved`: the difference is the 441 `open`
    reviews, and every one of a 100-ticket sample carried an assignee, a group and
    an updated_at later than its created_at. Something has already handled those,
    so they are not this job's to close.
    """
    return "type:ticket status:new via:any_channel order_by:created_at sort:desc"


def select_resolvable(tickets, min_stars):
    """Split fetched tickets into (resolvable, skipped) with a reason per skip.

    Every ticket is re-checked here rather than trusted from the query: the query
    cannot express the star rating, and a mis-typed query must not turn into a bulk
    status change over real tickets.
    """
    resolvable, skipped = [], []
    for ticket in tickets:
        if not triage.is_store_review(ticket):
            skipped.append((ticket, "not an app-store review"))
            continue
        stars = triage.review_stars(ticket)
        if stars is None:
            skipped.append((ticket, "no star rating in the subject"))
            continue
        if stars < min_stars:
            skipped.append((ticket, f"{stars}★ is below the {min_stars}★ floor"))
            continue
        resolvable.append(ticket)
    return resolvable, skipped


def batches(items, size=BATCH_SIZE):
    for start in range(0, len(items), size):
        yield items[start : start + size]


def solve_batch(session, subdomain, ids, tag, note):
    """PUT one batch of up to 100 ids to solved; return the job id.

    `additional_tags` rather than `tags`, which would replace the ticket's tags
    instead of adding to them. The note is private, so closing a review cannot mail
    the person who wrote it.
    """
    url = f"https://{subdomain}.zendesk.com/api/v2/tickets/update_many.json"
    payload = {"ticket": {"status": "solved", "additional_tags": [tag]}}
    if note:
        payload["ticket"]["comment"] = {"body": note, "public": False}
    resp = triage.request_with_retry(
        session, "PUT", url, params={"ids": ",".join(str(i) for i in ids)}, json=payload
    )
    if resp.status_code >= 400:
        sys.exit(f"update_many failed ({resp.status_code}): {resp.text[:300]}")
    job = (resp.json() or {}).get("job_status") or {}
    job_id = job.get("id")
    if not job_id:
        sys.exit(f"update_many returned no job id: {str(resp.text)[:300]}")
    return job_id


def wait_for_job(session, subdomain, job_id, timeout=JOB_TIMEOUT_SECONDS):
    """Poll a bulk-update job to completion; return (solved_ids, failures).

    update_many is asynchronous, so a 200 on the PUT only means Zendesk queued the
    work. Without this a run would report success for tickets that failed to update.

    Ids rather than a count, because the Discord summary tallies the solved tickets
    by star rating and has to know which ones landed. A result entry without an id
    can't be attributed to a ticket so it isn't counted — undercounting is the safe
    direction for a message that claims what was changed.
    """
    url = f"https://{subdomain}.zendesk.com/api/v2/job_statuses/{job_id}.json"
    deadline = time.monotonic() + timeout
    while True:
        resp = triage.request_with_retry(session, "GET", url)
        if resp.status_code >= 400:
            sys.exit(f"could not read job {job_id} ({resp.status_code}): {resp.text[:200]}")
        job = (resp.json() or {}).get("job_status") or {}
        status = job.get("status")
        if status in ("completed", "failed", "killed"):
            results = job.get("results") or []
            failures = [r for r in results if r.get("success") is False or r.get("error")]
            solved = [r.get("id") for r in results
                      if r.get("success") is not False and not r.get("error")
                      and r.get("id") is not None]
            if status != "completed":
                print(f"Job {job_id} ended as {status}.")
            return solved, failures
        if time.monotonic() >= deadline:
            sys.exit(f"Job {job_id} still {status} after {timeout}s; "
                     f"check it in Zendesk before re-running.")
        time.sleep(JOB_POLL_SECONDS)


# ---- Discord summary -------------------------------------------------------
#
# One plain message, same channel and same no-embed style as the daily triage. It is
# a handful of lines by construction — a tally, not a per-ticket list — so unlike the
# triage it never needs clipping or chunking against Discord's 2,000-character cap.


def tally_by_stars(tickets):
    """{stars: count} over already-selected reviews, so the rating breakdown is
    reported rather than a bare total: 4★ and 5★ are different enough that seeing
    the split is what makes the number worth reading."""
    counts = {}
    for ticket in tickets:
        # Never None here: select_resolvable drops anything without a parsed rating.
        stars = triage.review_stars(ticket)
        counts[stars] = counts.get(stars, 0) + 1
    return counts


def format_tally(counts):
    """{4: 12, 5: 31} -> "**12** 4★ and **31** 5★", ascending."""
    parts = [f"**{count:,}** {stars}★" for stars, count in sorted(counts.items())]
    if len(parts) > 1:
        return ", ".join(parts[:-1]) + f" and {parts[-1]}"
    return parts[0] if parts else ""


def build_message(counts, remaining=0, failures=0, dry_run=False):
    """The run summary, or None when there is nothing worth saying.

    Nothing solved and nothing failed means silence: a weekly job that posts "0
    reviews" every week is noise the channel learns to skip past, and the backlog
    running dry is exactly when that would start happening.
    """
    total = sum(counts.values())
    if not total and not failures:
        return None
    lines = []
    if total:
        prefix = "Would mark" if dry_run else "Marked"
        noun = "review" if total == 1 else "reviews"
        lines.append(f"✅ {prefix} {format_tally(counts)} app-store {noun} as solved "
                     f"in Zendesk.")
    if failures:
        # The run exits non-zero too, but the failure notification says only that a
        # workflow failed — the count belongs next to the number it contradicts.
        noun = "ticket" if failures == 1 else "tickets"
        lines.append(f"⚠️ **{failures:,}** {noun} failed to update — see the run log.")
    if remaining:
        # Query matches this run did not look at, not reviews: the query cannot
        # express the star rating, so some of these are not reviews at all.
        lines.append(f"📥 **{remaining:,}** more tickets match than this run looked "
                     f"at; the next run picks them up.")
    return "\n".join(lines)


def post_summary(webhook_url, message):
    """Post the summary; return whether Discord accepted it.

    A fresh session, never the Zendesk one — that carries the API-token auth header,
    and Discord has no business receiving it.
    """
    return bool(triage.post_to_discord(requests.Session(), webhook_url,
                                       [{"content": message}]))


def main():
    parser = argparse.ArgumentParser(
        description="Solve non-actionable positive app-store reviews in Zendesk.")
    parser.add_argument("--apply", action="store_true",
                        help="Actually solve the tickets. Without this the script "
                             "reports what it would do and changes nothing.")
    parser.add_argument("--max-tickets", type=int, default=DEFAULT_MAX_TICKETS, metavar="N",
                        help=f"Runaway guard on tickets solved per run "
                             f"(default: {DEFAULT_MAX_TICKETS}, Zendesk's search cap).")
    parser.add_argument("--tag", default=RESOLVED_TAG,
                        help=f"Tag added to every ticket solved, so they stay "
                             f"identifiable and a trigger can exclude them "
                             f"(default: {RESOLVED_TAG}).")
    parser.add_argument("--no-note", action="store_true",
                        help="Skip the private note explaining the automated close.")
    parser.add_argument("--no-discord", action="store_true",
                        help="Solve the tickets without posting the summary to Discord.")
    parser.add_argument("--subdomain", help="Zendesk subdomain (else ZENDESK_SUBDOMAIN).")
    parser.add_argument("--email", help="Zendesk agent email (else ZENDESK_EMAIL).")
    parser.add_argument("--api-token", help="Zendesk API token (else ZENDESK_API_TOKEN).")
    parser.add_argument("--webhook", help="Discord webhook URL (else DISCORD_WEBHOOK_URL).")
    args = parser.parse_args()

    subdomain = triage.get_env("ZENDESK_SUBDOMAIN", args.subdomain)
    email = triage.get_env("ZENDESK_EMAIL", args.email)
    api_token = triage.get_env("ZENDESK_API_TOKEN", args.api_token)
    # Resolved up front, before anything is fetched or solved: a missing webhook
    # should stop the run rather than have it bulk-edit tickets it cannot report. A
    # dry run posts nothing, so it never needs one.
    needs_webhook = args.apply and not args.no_discord
    webhook = triage.get_env("DISCORD_WEBHOOK_URL", args.webhook, required=needs_webhook)

    session = triage.zendesk_session(email, api_token)
    query = build_query()
    tickets, total_matched = triage.fetch_tickets(session, subdomain, query, args.max_tickets)
    matched = "?" if total_matched is None else total_matched
    print(f"Fetched {len(tickets)} of {matched} matching tickets (query: {query!r}).")

    resolvable, skipped = select_resolvable(tickets, MIN_STARS)
    if skipped:
        reasons = {}
        for _, reason in skipped:
            reasons[reason] = reasons.get(reason, 0) + 1
        for reason, count in sorted(reasons.items(), key=lambda kv: -kv[1]):
            print(f"  skipped {count}: {reason}")
    if not resolvable:
        print("Nothing to solve.")
        return

    print(f"{len(resolvable)} review(s) at {MIN_STARS}★ or better would be solved "
          f"and tagged {args.tag!r}.")
    remaining = 0
    if total_matched is not None and total_matched > len(tickets):
        remaining = total_matched - len(tickets)
        print(f"Note: {remaining} more match the query than this run "
              f"looked at; the next run picks them up.")

    if not args.apply:
        print("Dry run: nothing was changed. Re-run with --apply to solve them.")
        preview = build_message(tally_by_stars(resolvable), remaining, dry_run=True)
        print(f"Discord message that a real run would post:\n{preview}")
        return

    note = None if args.no_note else (
        f"Solved automatically: {MIN_STARS}★ or better app-store review with no "
        f"actionable content. See zendesk_triage/resolve_reviews.py.")

    solved_ids, failures = [], []
    ids = [t["id"] for t in resolvable]
    for number, batch in enumerate(batches(ids), start=1):
        print(f"  batch {number}: solving {len(batch)} ticket(s)…")
        job_id = solve_batch(session, subdomain, batch, args.tag, note)
        batch_solved, batch_failures = wait_for_job(session, subdomain, job_id)
        solved_ids.extend(batch_solved)
        failures.extend(batch_failures)

    print(f"Solved {len(solved_ids)} of {len(ids)} ticket(s).")

    # Tallied from the ids the jobs confirmed, not from what was submitted, so the
    # message reports what Zendesk actually changed.
    by_id = {t["id"]: t for t in resolvable}
    solved_tickets = [by_id[i] for i in solved_ids if i in by_id]
    message = build_message(tally_by_stars(solved_tickets), remaining, len(failures))
    posted = True
    if message and args.no_discord:
        print(f"Discord post skipped (--no-discord):\n{message}")
    elif message:
        posted = post_summary(webhook, message)

    if failures:
        for failure in failures[:10]:
            print(f"  failed: id={failure.get('id')} {failure.get('error') or failure}")
        sys.exit(f"{len(failures)} ticket(s) failed to update.")
    # Non-zero so the failure notification fires: the tickets are solved either way,
    # and re-running cannot recover the report — solved tickets leave the query.
    if not posted:
        sys.exit("Tickets were solved, but the Discord summary could not be posted.")


if __name__ == "__main__":
    main()
