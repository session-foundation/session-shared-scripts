"""Zendesk's API as the jobs use it: tickets, their comments and users, the
markers a run leaves, and what a customer's side of a ticket looks like.
"""
import re
import sys
from datetime import datetime

import requests

from session_ops.shared import http
from session_ops.shared.discord import clip
from session_ops.shared.text import squash


# The channel AppFollow imports app-store reviews on. Identified reviews with no
# false positives in a 3,662-ticket sample; tags did not (only 287 carried one).
REVIEW_CHANNEL = "any_channel"


# The Search API hard-caps a query at 1000 results and returns 422 for any page past
# it (at per_page=100 that is page 11), so pagination stops here rather than walking
# into that error. Above the cap the digest reports truncation — which it already does
# for --max-tickets — instead of failing the run.
# https://developer.zendesk.com/api-reference/ticketing/ticket-management/search/#results-limit
SEARCH_RESULT_LIMIT = 1000


def zendesk_session(email, token):
    session = http.Session()
    # Zendesk API-token auth: username is "{email}/token", password is the token.
    session.auth = (f"{email}/token", token)
    session.headers["Accept"] = "application/json"
    return session


def fetch_every_ticket(session, subdomain, query, max_tickets):
    """Fetch past the Search API's 1000-result ceiling, in created_at slices.

    The ceiling is per query, not per account: `created<=` the oldest result so far
    is a different query with a fresh 1000 of its own. `query` must order by
    created_at descending for that to hold.

    Without this, a query matching more than 1000 truncates at the newest 1000 and
    the tail is unreachable at any --max-tickets — permanently, when the surplus is
    tickets the caller never removes. The positive-review job hit exactly that: 1,036
    matches held open by 1,030 low-star reviews it will never solve, hiding six 4-5★
    ones from August 2025 that it would have.

    `created<=`, not `<`: created_at has second granularity, so `<` would skip every
    ticket sharing the oldest second. The overlap is re-fetched and dropped by id
    instead, and a slice that adds nothing new ends the walk — which is also what
    stops a tie group larger than a whole slice from looping forever.

    Returns (tickets, total_matched) like fetch_tickets, with total_matched from the
    unsliced query so the caller still reports the true gap.
    """
    tickets, seen, total_matched, cutoff = [], set(), None, None
    while len(tickets) < max_tickets:
        sliced = query if cutoff is None else f"{query} created<={cutoff}"
        batch, matched = fetch_tickets(session, subdomain, sliced,
                                       max_tickets - len(tickets))
        if total_matched is None:
            total_matched = matched
        fresh = [t for t in batch if t.get("id") not in seen]
        if not fresh:
            break
        seen.update(t.get("id") for t in fresh)
        tickets.extend(fresh)
        stamps = [t.get("created_at") for t in batch if t.get("created_at")]
        if not stamps or len(batch) < SEARCH_RESULT_LIMIT:
            break
        cutoff = min(stamps)
    return tickets[:max_tickets], total_matched


def fetch_tickets(session, subdomain, query, max_tickets):
    """Fetch tickets via the Zendesk Search API, following pagination.

    Returns (tickets, total_matched). total_matched is the full result count
    reported by Zendesk, which can exceed len(tickets) when max_tickets — or
    SEARCH_RESULT_LIMIT — caps the batch; the caller surfaces that gap so the
    truncation isn't silent.
    """
    base = f"https://{subdomain}.zendesk.com/api/v2/search.json"
    url = base
    params = {"query": query, "per_page": 100}
    tickets = []
    total_matched = None
    # Whichever bites first: our own runaway guard or Zendesk's hard result limit.
    cap = min(max_tickets, SEARCH_RESULT_LIMIT)
    while url and len(tickets) < cap:
        resp = session.request("GET", url, params=params)
        params = None  # next_page already carries the query
        if resp.status_code == 403:
            sys.exit("Zendesk returned 403 — the API token/email may lack search access.")
        # 422 past the result limit: `cap` should have stopped us first, so this only
        # fires if the account's effective limit is lower than documented. Keep the
        # tickets already in hand — a partial digest beats no digest — and let the
        # caller report the gap. With nothing in hand there is nothing to salvage.
        if resp.status_code == 422 and tickets:
            print(f"Note: Zendesk stopped paginating at {len(tickets)} results "
                  f"(search result limit); analyzing what was fetched.")
            break
        if resp.status_code >= 400:
            sys.exit(f"Zendesk search failed ({resp.status_code}): {resp.text[:300]}")
        payload = resp.json()
        if total_matched is None:
            total_matched = payload.get("count")
        for row in payload.get("results", []):
            if row.get("result_type") != "ticket":
                continue
            tickets.append(row)
            if len(tickets) >= cap:
                break
        url = payload.get("next_page")
    return tickets, total_matched


def fetch_ticket(session, subdomain, ticket_id):
    url = f"https://{subdomain}.zendesk.com/api/v2/tickets/{ticket_id}.json"
    resp = session.request("GET", url)
    if resp.status_code == 404:
        sys.exit(f"Ticket #{ticket_id} does not exist.")
    if resp.status_code >= 400:
        # A read error's body describes the error, not the ticket, so it is safe to
        # print here. The write path deliberately prints no body at all.
        sys.exit(f"Could not read ticket #{ticket_id} ({resp.status_code}): "
                 f"{resp.text[:200]}")
    return (resp.json() or {}).get("ticket") or {}


def fetch_comments(session, subdomain, ticket_id, per_page=100, order="desc",
                   optional=False):
    """One page of the ticket's comments, newest first unless `order` says otherwise;
    None leaves the order to Zendesk.

    Newest first by default because the marker that stops a re-run from writing twice
    will be on the most recent comment, and one page of a busy ticket would otherwise
    be all opening back-and-forth.

    `optional` is for an enrichment: on a short retry budget, and a failure prints a
    note and returns None rather than stopping the run.
    """
    url = f"https://{subdomain}.zendesk.com/api/v2/tickets/{ticket_id}/comments.json"
    params = {"per_page": per_page, **({"sort_order": order} if order else {})}
    if not optional:
        resp = session.request("GET", url, params=params)
        if resp.status_code >= 400:
            sys.exit(f"Could not read the comments on #{ticket_id} ({resp.status_code}).")
        return (resp.json() or {}).get("comments") or []
    try:
        resp = session.request("GET", url, attempts=2, params=params)
    except requests.RequestException as exc:
        print(f"Note: could not fetch comments for #{ticket_id} ({exc}).")
        return None
    if resp.status_code >= 400:
        print(f"Note: comments for #{ticket_id} returned {resp.status_code}.")
        return None
    try:
        return resp.json().get("comments", [])
    except ValueError as exc:
        # A 200 carrying an HTML error page (proxy, maintenance) is the same kind of
        # non-event as an HTTP error here.
        print(f"Note: unreadable comments payload for #{ticket_id} ({exc}).")
        return None


def hydrate_requester_activity(session, subdomain, tickets):
    """Fill `requester_updated_at` from each ticket's metric set. Returns the count.

    Sideloaded through show_many, so this is one request per 100 tickets rather than
    one per ticket.
    """
    hydrated = 0
    ids = [t["id"] for t in tickets if t.get("id") is not None]
    for start in range(0, len(ids), 100):
        chunk = ids[start : start + 100]
        url = f"https://{subdomain}.zendesk.com/api/v2/tickets/show_many.json"
        try:
            resp = session.request("GET", url, attempts=2, params={
                "ids": ",".join(str(i) for i in chunk), "include": "metric_sets"})
        except requests.RequestException as exc:
            print(f"Note: could not fetch ticket metrics ({exc}); "
                  f"falling back to updated_at for {len(chunk)} ticket(s).")
            continue
        if resp.status_code >= 400:
            print(f"Note: ticket metrics returned {resp.status_code}; "
                  f"falling back to updated_at for {len(chunk)} ticket(s).")
            continue
        try:
            metric_sets = resp.json().get("metric_sets", [])
        except ValueError as exc:
            print(f"Note: unreadable ticket metrics ({exc}); falling back to updated_at.")
            continue
        by_id = {m.get("ticket_id"): m.get("requester_updated_at") for m in metric_sets}
        for ticket in tickets:
            stamp = by_id.get(ticket.get("id"))
            if stamp:
                ticket["requester_updated_at"] = stamp
                hydrated += 1
    return hydrated


STAR_SUBJECT = re.compile(r"^\s*([★☆]{1,10})")


def marker(kind, value):
    """A machine-readable marker for a ticket comment.

    One shape for all of them: `[kind:value]`, matched by has_marker. What it is for
    is idempotency — a marker on the ticket says the work behind it is already done,
    so a replayed webhook or a re-run writes nothing a second time.

    The kind carries its own namespace (`discord`, `claude:done`) so the strings are
    byte-identical to the four hand-rolled versions this replaces. That matters:
    markers are already written into real tickets, and a changed format would stop
    matching them and let a replay send twice.
    """
    return f"[{kind}:{value}]"


def has_marker(comments, wanted):
    """Whether any comment already carries this marker."""
    return any(wanted in (comment.get("body") or "") for comment in comments)


AGENT_ROLES = ("agent", "admin")


def fetch_user(session, subdomain, user_id):
    """One Zendesk user, or {} when it cannot be read.

    An author we cannot resolve is treated as a customer by customer_authors, so a
    failed lookup widens the sample rather than silencing it.
    """
    url = f"https://{subdomain}.zendesk.com/api/v2/users/{user_id}.json"
    try:
        resp = session.request("GET", url, attempts=2)
        if resp.status_code >= 400:
            return {}
        return (resp.json() or {}).get("user") or {}
    except requests.RequestException:
        return {}


def customer_authors(session, subdomain, ticket, comments):
    """The author ids on the customer's side of this ticket.

    Deciding by `requester_id` alone is right for email and web tickets and wrong for
    every channel integration: on a Twitter or Sunshine DM the integration authors the
    customer's own message under its id, so the requester appears to have written
    nothing. That dropped every word a Chinese reviewer wrote and had them answered in
    English, and it labelled their message "Support" in the English transcript.

    So: the requester when they wrote anything, and otherwise everyone who is not an
    agent here. Roles are looked up rather than inferred from the id, because the
    integration's id is an account detail and an author we cannot resolve is a
    customer, not an agent.

    Ordinary tickets cost no extra API calls at all — the requester wrote something,
    and the lookup never happens.
    """
    requester = ticket.get("requester_id")
    if any(c.get("author_id") == requester for c in comments):
        return {requester}
    roles, customers = {}, set()
    for comment in comments:
        author = comment.get("author_id")
        if author not in roles:
            roles[author] = (fetch_user(session, subdomain, author) or {}).get("role")
        if roles[author] not in AGENT_ROLES:
            customers.add(author)
    return customers or {requester}


def customer_text(session, subdomain, ticket, comments, limit):
    """What the customer wrote, as the signal for which language to reply in.

    Their words only. An agent's earlier English reply is still text on the ticket,
    and including it would drag detection towards English on exactly the tickets this
    exists for.
    """
    # Public only. A private note is internal annotation — including the `claude:`
    # commands and the drafts this tool writes — and never the customer speaking.
    comments = [c for c in comments if c.get("public")]
    authors = customer_authors(session, subdomain, ticket, comments)
    subject = squash(ticket.get("subject"))
    parts = []
    description = (ticket.get("description") or "").strip()
    # A channel integration puts "Conversation with <handle>" here, which is the
    # ticket's own boilerplate rather than anything the customer typed.
    if description and squash(description) != subject:
        parts.append(description)
    for comment in reversed(comments):          # oldest first, so it reads in order
        if comment.get("author_id") not in authors:
            continue
        body = (comment.get("body") or "").strip()
        if body and squash(body) != subject and body not in parts:
            parts.append(body)
    # A ticket can carry no text at all — an attachment, or an import that lost its
    # body. Say so rather than sending an empty sample, which reads as a blank
    # question the model has to answer anyway.
    return clip("\n\n".join(parts), limit) or "(no text)"


def review_stars(ticket):
    """Star count from an AppFollow review subject, or None if not a review subject."""
    match = STAR_SUBJECT.match(ticket.get("subject") or "")
    return match.group(1).count("★") if match else None


def is_store_review(ticket):
    return (((ticket.get("via") or {}).get("channel") == REVIEW_CHANNEL)
            or STAR_SUBJECT.match(ticket.get("subject") or "") is not None)


# Zendesk names the integration that imported a review under `via.source.from`, and
# that name is the store it came from. Both names are searched because the two
# integrations put the store in different ones: Google Play is the registered
# service name itself, while the App Store's registered name is the generic
# "AppFollow: Review Monitor" and only the instance name — "AppFollow (Session -
# Private Messenger, App Store)" — says which store. Across 5,113 sampled reviews
# spanning 2022-2026 these were the only two integrations, and both named the store
# on every ticket.
REVIEW_SOURCE_PLATFORMS = (("google play", "android"), ("app store", "ios"))
REVIEW_SOURCE_NAME_FIELDS = ("registered_integration_service_name",
                             "integration_service_instance_name")


def review_platform(ticket):
    """Store an app-store review was imported from, as a PLATFORMS value.

    None when the ticket is not a review or its source names no store we know, which
    leaves the model's guess in place rather than replacing it with 'unknown'.
    """
    if not is_store_review(ticket):
        return None
    source = ((ticket.get("via") or {}).get("source") or {}).get("from") or {}
    service = source.get("service_info") or {}
    names = " ".join(str(service.get(field) or "")
                     for field in REVIEW_SOURCE_NAME_FIELDS).lower()
    for needle, platform in REVIEW_SOURCE_PLATFORMS:
        if needle in names:
            return platform
    return None


# Private notes are left out. They are internal annotation rather than conversation,
# they are already English — note_reply.py's own attribution notes among them — and
# translating its `[discord:…]` markers back would put bookkeeping in front of an
# agent as if the customer had said it.
CUSTOMER_TURN = "Customer"
SUPPORT_TURN = "Support"


def conversation_turns(session, subdomain, ticket):
    """One ticket's public comments as turns, oldest first. None on any failure.

    Both sides, not just the requester's. A customer's second message is usually an
    answer to a reply, and dropping the reply leaves "still broken" sitting under the
    original complaint with nothing visible for it to be answering.

    Who spoke is decided by customer_authors, not by `requester_id` alone: on a
    Twitter or Sunshine DM the integration authors the customer's message under its
    own id, and comparing against the requester labelled their words "Support" in the
    transcript an agent then read. A transcript that mislabels who spoke is worse than
    none.
    """
    requester = ticket.get("requester_id")
    if requester is None:
        print(f"Note: #{ticket['id']} has no requester_id; skipping its transcript.")
        return None
    comments = fetch_comments(session, subdomain, ticket["id"], order="asc", optional=True)
    if comments is None:
        return None
    public = [c for c in comments if c.get("public")]
    authors = customer_authors(session, subdomain, ticket, public)
    turns = []
    for comment in public:
        body = (comment.get("body") or "").strip()
        if not body:
            continue
        turns.append({
            "index": len(turns),
            "who": (CUSTOMER_TURN if comment.get("author_id") in authors
                    else SUPPORT_TURN),
            "when": stamp_minutes(comment.get("created_at")),
            "body": body,
        })
    return turns or None


def stamp_minutes(created_at):
    """Zendesk's ISO timestamp as `2026-08-28 01:31 UTC`, or '' if unparseable.

    Minutes, not seconds: this dates a turn for somebody reading a conversation, and
    the extra precision is noise in front of every paragraph.
    """
    try:
        when = datetime.strptime(created_at, "%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError):
        return ""
    return when.strftime("%Y-%m-%d %H:%M UTC")


def ticket_url(subdomain, ticket_id):
    return f"https://{subdomain}.zendesk.com/agent/tickets/{ticket_id}"
