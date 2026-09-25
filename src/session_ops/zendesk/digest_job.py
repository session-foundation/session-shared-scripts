"""The weekday Zendesk run: solve positive app-store reviews, then post the digest.

In that order because a solved review leaves the digest's `status<pending` query, so
resolving first is what stops the digest counting reviews just closed. The resolver
is an optimisation for the digest rather than a precondition for it, so its failure
is reported and the digest runs anyway.

    session-ops run zendesk-digest [--dry-run]
"""
import argparse

from session_ops.ops.runner import Outcome, call_target, step
from session_ops.zendesk import resolve_reviews, triage

RESOLVER, DIGEST = "resolve reviews", "digest"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--state", metavar="PATH", help="The digest's dedup state.")
    parser.add_argument("--window-hours", type=int, default=72)
    parser.add_argument("--dry-run", action="store_true",
                        help="Resolve nothing and post nothing; print the digest.")
    args = parser.parse_args(argv)

    step(RESOLVER)
    resolved = call_target(resolve_reviews.main,
                           ["--no-discord"] if args.dry_run else ["--apply"])
    step(DIGEST)
    digest = ["--window-hours", str(args.window_hours)]
    if args.state:
        digest += ["--state", args.state]
    digested = call_target(triage.main, digest + (["--dry-run"] if args.dry_run else []))
    return Outcome(targets={RESOLVER: resolved, DIGEST: digested},
                   optional=frozenset({RESOLVER}))
