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
  * with a parsed rating at or above --min-stars (default 4). A review whose stars
    cannot be parsed is skipped, never solved
  * in `new` only, unless --include-open. 441 reviews are `open`, which can mean an
    agent engaged with one, and this job has no business closing that
  * `solved`, never `closed` — solved is reversible, closed is not

No state file: solved tickets drop out of the query, so runs are idempotent and a
weekly schedule drains the backlog and then keeps pace with new reviews.

Config (env vars, or flags for local runs):
    ZENDESK_SUBDOMAIN     e.g. "mycompany"  -> https://mycompany.zendesk.com
    ZENDESK_EMAIL         agent email for API token auth
    ZENDESK_API_TOKEN     Zendesk API token

Usage:
    # report what would be solved, touch nothing (the default)
    python resolve_reviews.py

    # actually solve them
    python resolve_reviews.py --apply

    # first real run: small, observable
    python resolve_reviews.py --apply --max-tickets 5
"""
import argparse
import os
import sys
import time

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import triage  # noqa: E402  (needs the path insert above)

# Reviews at or above this rating carry nothing to act on. 3★ and below are left
# alone: the triage treats them as bug reports in disguise.
DEFAULT_MIN_STARS = 4
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


def build_query(include_open):
    """Unsolved app-store reviews, newest first.

    `via:any_channel` is what AppFollow imports arrive on, and it is the cheap half
    of the filter — the star rating lives in the subject, which Zendesk's search
    index will not match, so the rating is applied locally in select_resolvable.
    """
    status = "status<solved" if include_open else "status:new"
    return f"type:ticket {status} via:any_channel order_by:created_at sort:desc"


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
    """Poll a bulk-update job to completion; return (solved_count, failures).

    update_many is asynchronous, so a 200 on the PUT only means Zendesk queued the
    work. Without this a run would report success for tickets that failed to update.
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
            solved = [r for r in results if r.get("success") is not False and not r.get("error")]
            if status != "completed":
                print(f"Job {job_id} ended as {status}.")
            return len(solved), failures
        if time.monotonic() >= deadline:
            sys.exit(f"Job {job_id} still {status} after {timeout}s; "
                     f"check it in Zendesk before re-running.")
        time.sleep(JOB_POLL_SECONDS)


def main():
    parser = argparse.ArgumentParser(
        description="Solve non-actionable positive app-store reviews in Zendesk.")
    parser.add_argument("--apply", action="store_true",
                        help="Actually solve the tickets. Without this the script "
                             "reports what it would do and changes nothing.")
    parser.add_argument("--min-stars", type=int, default=DEFAULT_MIN_STARS, metavar="N",
                        help=f"Only solve reviews rated N stars or better "
                             f"(default: {DEFAULT_MIN_STARS}).")
    parser.add_argument("--max-tickets", type=int, default=DEFAULT_MAX_TICKETS, metavar="N",
                        help=f"Runaway guard on tickets solved per run "
                             f"(default: {DEFAULT_MAX_TICKETS}, Zendesk's search cap).")
    parser.add_argument("--include-open", action="store_true",
                        help="Also consider `open` reviews, not just `new`. An open "
                             "ticket may have had agent activity.")
    parser.add_argument("--tag", default=RESOLVED_TAG,
                        help=f"Tag added to every ticket solved, so they stay "
                             f"identifiable and a trigger can exclude them "
                             f"(default: {RESOLVED_TAG}).")
    parser.add_argument("--no-note", action="store_true",
                        help="Skip the private note explaining the automated close.")
    parser.add_argument("--subdomain", help="Zendesk subdomain (else ZENDESK_SUBDOMAIN).")
    parser.add_argument("--email", help="Zendesk agent email (else ZENDESK_EMAIL).")
    parser.add_argument("--api-token", help="Zendesk API token (else ZENDESK_API_TOKEN).")
    args = parser.parse_args()

    if args.min_stars < 1 or args.min_stars > 5:
        sys.exit("--min-stars must be between 1 and 5.")

    subdomain = triage.get_env("ZENDESK_SUBDOMAIN", args.subdomain)
    email = triage.get_env("ZENDESK_EMAIL", args.email)
    api_token = triage.get_env("ZENDESK_API_TOKEN", args.api_token)

    session = triage.zendesk_session(email, api_token)
    query = build_query(args.include_open)
    tickets, total_matched = triage.fetch_tickets(session, subdomain, query, args.max_tickets)
    matched = "?" if total_matched is None else total_matched
    print(f"Fetched {len(tickets)} of {matched} matching tickets (query: {query!r}).")

    resolvable, skipped = select_resolvable(tickets, args.min_stars)
    if skipped:
        reasons = {}
        for _, reason in skipped:
            reasons[reason] = reasons.get(reason, 0) + 1
        for reason, count in sorted(reasons.items(), key=lambda kv: -kv[1]):
            print(f"  skipped {count}: {reason}")
    if not resolvable:
        print("Nothing to solve.")
        return

    print(f"{len(resolvable)} review(s) at {args.min_stars}★ or better would be solved "
          f"and tagged {args.tag!r}.")
    if total_matched is not None and total_matched > len(tickets):
        print(f"Note: {total_matched - len(tickets)} more match the query than this run "
              f"looked at; the next run picks them up.")

    if not args.apply:
        print("Dry run: nothing was changed. Re-run with --apply to solve them.")
        return

    note = None if args.no_note else (
        f"Solved automatically: {args.min_stars}★ or better app-store review with no "
        f"actionable content. See zendesk_triage/resolve_reviews.py.")

    solved_total, failures = 0, []
    ids = [t["id"] for t in resolvable]
    for number, batch in enumerate(batches(ids), start=1):
        print(f"  batch {number}: solving {len(batch)} ticket(s)…")
        job_id = solve_batch(session, subdomain, batch, args.tag, note)
        solved, batch_failures = wait_for_job(session, subdomain, job_id)
        solved_total += solved
        failures.extend(batch_failures)

    print(f"Solved {solved_total} of {len(ids)} ticket(s).")
    if failures:
        for failure in failures[:10]:
            print(f"  failed: id={failure.get('id')} {failure.get('error') or failure}")
        sys.exit(f"{len(failures)} ticket(s) failed to update.")


if __name__ == "__main__":
    main()
