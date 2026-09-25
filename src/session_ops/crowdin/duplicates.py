"""Crowdin string slots holding more than one translation: finding them, remembering
which are open, and telling Discord what changed.

A slot is (string, locale, plural category). Exactly one translation per slot is the
goal, so a slot with two or more needs someone to choose the keeper.

The state is the set of open slots. Two writers keep it current: the relay, one
(string, locale) at a time as Crowdin reports suggestions, and reconciliation, over
every string of every locale it scans. Crowdin never retries a webhook, so
reconciliation is what makes the state correct; the relay only makes it prompt.
Each writer records when it checked each (string, locale), and the newer check wins,
so a scan that began before an event cannot undo what the event found.
"""
import collections
import contextlib
import fcntl
import json
import os
import time

from session_ops.crowdin import sdk
from session_ops.shared.discord import clip

STATE_VERSION = 1
MAX_EMBEDS_PER_MESSAGE = 10
MAX_DESC_CHARS = 3800
MAX_MESSAGE_CHARS = 6000
OPEN_COLOR, RESOLVED_COLOR = 0xE67E22, 0x2ECC71


def user_label(u):
    if not u:
        return "<none/MT>"
    return f"{u.get('id')}:{u.get('username') or u.get('fullName') or '?'}"


def slots_for_string(translations, approved_ids, lang, sid, meta, web_url):
    """The finding for every plural category of one string holding 2+ translations."""
    by_cat = collections.defaultdict(list)
    for t in translations:
        by_cat[t.get("pluralCategoryName")].append(t)
    found = []
    for cat, ts in by_cat.items():
        if len(ts) < 2:
            continue
        ts.sort(key=lambda t: t.get("createdAt") or "")
        found.append({
            "locale": lang,
            "status": "multiple-translations",
            "stringId": sid,
            "identifier": meta.get("identifier"),
            "webUrl": web_url,
            "pluralCategory": cat,
            "count": len(ts),
            "sourceText": meta.get("text"),
            "translations": [{
                "translationId": t["id"],
                "user": user_label(t.get("user")),
                "createdAt": t.get("createdAt"),
                "approved": t["id"] in approved_ids,
                "rating": t.get("rating"),
                "isPreTranslated": t.get("isPreTranslated"),
                "provider": t.get("provider"),
                "text": t.get("text"),
            } for t in ts],
        })
    return found


def check_string(client, sid, lang, meta, web_url, approved_ids=None):
    """Fetch one (string, locale) and return its open slots. `approved_ids` saves a
    request when the caller already listed the locale's approvals."""
    translations = sdk.fetch_all(client.string_translations, "list_string_translations",
                                 stringId=sid, languageId=lang)
    if approved_ids is None:
        approved_ids = {a["translationId"] for a in sdk.fetch_all(
            client.string_translations, "list_translation_approvals",
            stringId=sid, languageId=lang)}
    return slots_for_string(translations, approved_ids, lang, sid, meta, web_url)


class Project:
    """What building editor links needs: the project slug and per-locale editor codes.
    A string's own webUrl always targets the first target language."""

    def __init__(self, details):
        self.slug = details["identifier"]
        source = details["sourceLanguage"]
        self.source_code = source.get("editorCode") or source["id"]
        self.editor_code = {lang["id"]: lang.get("editorCode") or lang["id"]
                            for lang in details["targetLanguages"]}
        self.locales = details["targetLanguageIds"]

    def editor_url(self, lang, sid):
        return (f"https://crowdin.com/editor/{self.slug}/all/"
                f"{self.source_code}-{self.editor_code.get(lang, lang)}#{sid}")


# ---- State -------------------------------------------------------------------


def slot_key(sid, lang, category):
    return f"{sid}:{lang}:{category or ''}"


def scope_key(sid, lang):
    return f"{sid}:{lang}"


def empty_state():
    return {"version": STATE_VERSION, "slots": {}, "checked": {}}


def load(path):
    """The state at `path`. Unlike a digest's dedup file, a lost one is not harmless:
    every open slot would be reported as new, so an unreadable file stops the run."""
    if not os.path.exists(path):
        return empty_state()
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    if data.get("version") != STATE_VERSION:
        raise SystemExit(f"{path} is version {data.get('version')!r}, expected "
                         f"{STATE_VERSION}; move it aside and --seed again.")
    return data


def save(path, state):
    temporary = f"{path}.tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=1, sort_keys=True)
    os.replace(temporary, path)


@contextlib.contextmanager
def locked(path):
    """Hold the state's lock: the relay and reconciliation run as separate processes."""
    with open(f"{path}.lock", "w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def record(finding, now):
    return {"stringId": finding["stringId"], "locale": finding["locale"],
            "pluralCategory": finding["pluralCategory"],
            "identifier": finding["identifier"], "count": finding["count"],
            "opened_at": now}


def apply(state, findings, checked, now, remember):
    """Merge a check into the state. Returns (opened, resolved) slot records.

    `checked` maps scope_key -> when that (string, locale) was fetched. Only those
    scopes are judged, and only where the state holds no newer check: a slot outside
    them, or checked later by someone else, is left exactly as it is.

    `remember` keeps the check times, which only matters while a reconciliation that
    started earlier may still be scanning: the relay's checks, not a scan's own.
    """
    newer = state["checked"]
    judged = {scope for scope, at in checked.items() if at >= newer.get(scope, 0)}
    current = {slot_key(f["stringId"], f["locale"], f["pluralCategory"]): f
               for f in findings if scope_key(f["stringId"], f["locale"]) in judged}
    opened, resolved = [], []
    for key, slot in list(state["slots"].items()):
        if scope_key(slot["stringId"], slot["locale"]) in judged and key not in current:
            resolved.append(state["slots"].pop(key))
    for key, finding in current.items():
        if key in state["slots"]:
            state["slots"][key]["count"] = finding["count"]
        else:
            state["slots"][key] = record(finding, now)
            opened.append(state["slots"][key])
    if remember:
        for scope in judged:
            newer[scope] = checked[scope]
    return opened, resolved


def forget_checks_before(state, cutoff):
    """Drop the check times no scan still running can predate: a reconciliation that
    started at `cutoff` has applied, and the next starts later."""
    state["checked"] = {k: at for k, at in state["checked"].items() if at > cutoff}


# ---- Discord -----------------------------------------------------------------


def slot_line(slot, project, suffix):
    cat = slot["pluralCategory"]
    cat_txt = f" `[{cat}]`" if cat and cat != "other" else ""
    ident = clip(slot["identifier"] or f"string {slot['stringId']}", 90)
    url = project.editor_url(slot["locale"], slot["stringId"])
    return f"• [{ident}]({url}){cat_txt}{suffix(slot)}"


def section_embeds(slots, project, title, color, suffix):
    """One embed per locale, split when a locale outgrows a description."""
    embeds = []
    by_locale = collections.defaultdict(list)
    for slot in slots:
        by_locale[slot["locale"]].append(slot)
    for lang in sorted(by_locale, key=lambda lang: (-len(by_locale[lang]), lang)):
        items = sorted(by_locale[lang], key=lambda s: (s["identifier"] or "",
                                                       str(s["pluralCategory"])))
        lines, used, first = [], 0, True
        for slot in items:
            line = slot_line(slot, project, suffix)
            if lines and used + len(line) + 1 > MAX_DESC_CHARS:
                embeds.append({"title": title(lang, len(items)) if first else f"{lang} (cont.)",
                               "description": "\n".join(lines), "color": color})
                lines, used, first = [], 0, False
            lines.append(line)
            used += len(line) + 1
        embeds.append({"title": title(lang, len(items)) if first else f"{lang} (cont.)",
                       "description": "\n".join(lines), "color": color})
    return embeds


def build_messages(opened, resolved, still_open, project):
    """Discord payloads for what changed, or [] when nothing did."""
    if not opened and not resolved:
        return []
    summary = {
        "title": "🈳 Crowdin: translations to choose between",
        "description": (
            f"**{len(opened)}** slot(s) newly holding 2+ translations, "
            f"**{len(resolved)}** resolved. **{still_open}** open in total.\n"
            "Each open slot needs one translation kept and the rest deleted, so that "
            "exactly one translation (per plural form) is exported."),
        "color": OPEN_COLOR if opened else RESOLVED_COLOR,
    }
    embeds = [summary]
    embeds += section_embeds(opened, project, lambda lang, n: f"{lang} — {n} new",
                             OPEN_COLOR, lambda s: f" — **{s['count']}** translations")
    embeds += section_embeds(resolved, project, lambda lang, n: f"{lang} — {n} resolved",
                             RESOLVED_COLOR, lambda s: "")
    return pack_embeds(embeds)


def embed_len(e):
    total = len(e.get("title") or "") + len(e.get("description") or "")
    for fld in e.get("fields", []):
        total += len(fld.get("name") or "") + len(fld.get("value") or "")
    return total


def pack_embeds(embeds):
    """Messages within Discord's 10 embeds and 6000 characters apiece."""
    messages, chunk, used = [], [], 0
    for e in embeds:
        size = embed_len(e)
        if chunk and (len(chunk) >= MAX_EMBEDS_PER_MESSAGE or used + size > MAX_MESSAGE_CHARS):
            messages.append({"embeds": chunk})
            chunk, used = [], 0
        chunk.append(e)
        used += size
    if chunk:
        messages.append({"embeds": chunk})
    return messages


def now():
    return time.time()
