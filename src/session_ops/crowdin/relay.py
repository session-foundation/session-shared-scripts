"""
The endpoint Crowdin posts suggestion webhooks to. Each event names a string and a
locale; the relay acknowledges at once, then re-checks that one (string, locale) and
posts any slot it opened or resolved.

Crowdin gives up on a delivery that is not answered 2xx within 30 seconds and never
retries it, so the check runs after the response. What it misses anyway, the
reconciliation timer finds.

Crowdin signs nothing, so the secret is in the URL: the webhook is configured as
https://<host>/crowdin/suggestions/<CROWDIN_WEBHOOK_SECRET>. A wrong or unset secret
gets a 404, the same as a route that does not exist.

Its own process and account rather than a route on the Zendesk relay: that one can
write public comments on any ticket, and the Crowdin token does not belong beside it.

Config (env vars):
    CROWDIN_API_TOKEN            read-only Crowdin token
    CROWDIN_DISCORD_WEBHOOK_URL  where changes go
    CROWDIN_WEBHOOK_SECRET       the last path segment Crowdin posts to
    CROWDIN_DUPLICATES_STATE     the reconciliation's --state file
    CROWDIN_PROJECT_ID           (optional) defaults to 618696
    CROWDIN_RELAY_DRY_RUN        (optional) "1" prints what it would post, writes nothing

    uvicorn session_ops.crowdin.relay:app --host 127.0.0.1 --port 8081
"""
import functools
import hmac
import json
import os
import sys

from fastapi import BackgroundTasks, FastAPI, Request, Response
from starlette.concurrency import run_in_threadpool

from session_ops.crowdin import duplicates, sdk
from session_ops.crowdin.reconcile import DEFAULT_PROJECT, crowdin_client
from session_ops.shared import discord, http

EVENTS = frozenset({"suggestion.added", "suggestion.updated", "suggestion.deleted",
                    "suggestion.approved", "suggestion.disapproved"})

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


def scopes_from(payload):
    """The (string id, locale) pairs a delivery names, batched or not.

    Payload fields are read defensively: an event this cannot place is dropped, and
    reconciliation covers it.
    """
    events = payload.get("events") if isinstance(payload, dict) and "events" in payload \
        else [payload]
    scopes = set()
    for event in events if isinstance(events, list) else []:
        if not isinstance(event, dict) or event.get("event") not in EVENTS:
            continue
        translation = event.get("translation") or {}
        string = translation.get("sourceString") or translation.get("string") or {}
        lang = (translation.get("targetLanguage") or {}).get("id")
        try:
            sid = int(string.get("id"))
        except (TypeError, ValueError):
            continue
        if lang:
            scopes.add((sid, str(lang)))
    return scopes


@functools.cache
def crowdin():
    client = crowdin_client(os.environ["CROWDIN_API_TOKEN"],
                            os.environ.get("CROWDIN_PROJECT_ID") or DEFAULT_PROJECT)
    return client, duplicates.Project(client.projects.get_project()["data"])


def check(sid, lang):
    """Re-check one (string, locale) and post what changed. Never raises: nothing is
    waiting on it, so the journal is where a failure goes."""
    state_path = os.environ["CROWDIN_DUPLICATES_STATE"]
    dry_run = os.environ.get("CROWDIN_RELAY_DRY_RUN") == "1"
    try:
        if not os.path.exists(state_path):
            print(f"No state at {state_path}: seed it before the relay acts.", flush=True)
            return
        client, project = crowdin()
        string = client.source_strings.get_string(stringId=sid)["data"]
        at = duplicates.now()
        found = duplicates.check_string(client, sid, lang, string,
                                        project.editor_url(lang, sid))
        with duplicates.locked(state_path):
            state = duplicates.load(state_path)
            opened, resolved = duplicates.apply(
                state, found, {duplicates.scope_key(sid, lang): at}, duplicates.now(),
                remember=True)
            messages = duplicates.build_messages(opened, resolved, len(state["slots"]),
                                                 project)
            if dry_run:
                print(json.dumps(messages, ensure_ascii=False), flush=True)
                return
            webhook = os.environ["CROWDIN_DISCORD_WEBHOOK_URL"]
            if messages and discord.post_to_discord(http.Session(), webhook,
                                                    messages) < len(messages):
                print(f"{sid}/{lang}: Discord refused; reconciliation will repeat it.",
                      flush=True)
                return
            duplicates.save(state_path, state)
        print(f"{sid}/{lang}: {len(opened)} opened, {len(resolved)} resolved", flush=True)
    except sdk.APIException as exc:
        print(f"{sid}/{lang}: Crowdin {exc.http_status} {sdk.error_message(exc)}",
              flush=True)
    except BaseException as exc:  # SystemExit included: this runs inside a server
        print(f"{sid}/{lang}: {exc!r}", file=sys.stderr, flush=True)


@app.post("/crowdin/suggestions/{secret}")
async def suggestions(secret: str, request: Request, background: BackgroundTasks):
    expected = os.environ.get("CROWDIN_WEBHOOK_SECRET", "")
    if not expected or not hmac.compare_digest(secret.encode(), expected.encode()):
        return Response(status_code=404)
    try:
        payload = await request.json()
    except ValueError:
        return Response(status_code=400)
    scopes = scopes_from(payload)
    if not scopes:
        print("A delivery named no suggestion this relay handles.", flush=True)
    for sid, lang in sorted(scopes):
        background.add_task(run_in_threadpool, check, sid, lang)
    return {"ok": True, "checks": len(scopes)}


@app.get("/healthz")
async def healthz():
    return {"ok": True}
