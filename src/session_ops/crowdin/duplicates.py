"""Crowdin string slots holding more than one translation: finding them, remembering
which are open, and telling Discord what changed.

A slot is (string, locale, plural category). Exactly one translation per slot is the
goal, so a slot with two or more needs someone to choose the keeper.

The state is the set of open slots, written only by the daily reconciliation.
"""
import collections
import contextlib
import fcntl
import json
import os
import time

from session_ops.crowdin import sdk
from session_ops.shared import discord
from session_ops.shared.discord import clip

STATE_VERSION = 1
OPEN_COLOR, RESOLVED_COLOR = 0xE67E22, 0x2ECC71
# A locale's slots listed one by one; past this, a count and the locale's editor. A burst
# (a bulk import, a whole locale reviewed at once) then stays a message, not a flood that
# outruns Discord's per-channel rate limit.
LISTED_PER_LOCALE = 10


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


def check_string(client, sid, lang, meta, web_url, approved_ids):
    """Fetch one (string, locale) and return its open slots."""
    translations = sdk.fetch_all(client.string_translations, "list_string_translations",
                                 stringId=sid, languageId=lang)
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

    def locale_url(self, lang):
        return (f"https://crowdin.com/editor/{self.slug}/all/"
                f"{self.source_code}-{self.editor_code.get(lang, lang)}")

    def editor_url(self, lang, sid):
        return f"{self.locale_url(lang)}#{sid}"


# ---- State -------------------------------------------------------------------


def slot_key(sid, lang, category):
    return f"{sid}:{lang}:{category or ''}"


def scope_key(sid, lang):
    return f"{sid}:{lang}"


def empty_state():
    return {"version": STATE_VERSION, "slots": {}}


def load(path, missing_ok=False):
    """The state at `path`. Unlike a digest's dedup file, a lost one is not harmless:
    every open slot would be reported as new, so a missing or unreadable file stops
    the run unless the caller is seeding it or writing nothing."""
    if not os.path.exists(path):
        if missing_ok:
            return empty_state()
        raise SystemExit(f"No state at {path}: record what is open first with --seed, "
                         f"or every open slot is posted as new.")
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    if data.get("version") != STATE_VERSION:
        raise SystemExit(f"{path} is version {data.get('version')!r}, expected "
                         f"{STATE_VERSION}; move it aside and --seed again.")
    return {"version": STATE_VERSION, "slots": data.get("slots", {})}


def save(path, state):
    temporary = f"{path}.tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=1, sort_keys=True)
    os.replace(temporary, path)


@contextlib.contextmanager
def only_run(path):
    """Refuse to start while another reconciliation holds `path`'s lock: two scans of
    different ages applied in either order would each undo the other's view."""
    with open(f"{path}.lock", "w") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit(f"Another reconciliation holds {path}.lock; not starting.")
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def record(finding, now):
    return {"stringId": finding["stringId"], "locale": finding["locale"],
            "pluralCategory": finding["pluralCategory"],
            "identifier": finding["identifier"], "count": finding["count"],
            "opened_at": now}


def apply(state, findings, judged, now):
    """Merge a scan into the state. Returns (opened, resolved) slot records.

    `judged` is the scope_keys whose every slot the scan saw. A slot outside them is
    left exactly as it is, so a string that failed to fetch keeps what it had.
    """
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
    return opened, resolved


# ---- Discord -----------------------------------------------------------------


def slot_line(slot, project, suffix):
    cat = slot["pluralCategory"]
    cat_txt = f" `[{cat}]`" if cat and cat != "other" else ""
    ident = clip(slot["identifier"] or f"string {slot['stringId']}", 90)
    url = project.editor_url(slot["locale"], slot["stringId"])
    return f"• [{ident}]({url}){cat_txt}{suffix(slot)}"


def section_embeds(slots, project, title, color, suffix):
    """One embed per locale: up to LISTED_PER_LOCALE slots, then how many more."""
    embeds = []
    by_locale = collections.defaultdict(list)
    for slot in slots:
        by_locale[slot["locale"]].append(slot)
    for lang in sorted(by_locale, key=lambda lang: (-len(by_locale[lang]), lang)):
        items = sorted(by_locale[lang], key=lambda s: (s["identifier"] or "",
                                                       str(s["pluralCategory"])))
        listed = items if len(items) <= LISTED_PER_LOCALE else items[:LISTED_PER_LOCALE - 1]
        lines = [slot_line(slot, project, suffix) for slot in listed]
        if len(listed) < len(items):
            lines.append(f"…and **{len(items) - len(listed)}** more: "
                         f"[open {lang} in the editor]({project.locale_url(lang)})")
        embeds.append({"title": title(lang, len(items)), "description": "\n".join(lines),
                       "color": color})
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
    return discord.pack_embeds(embeds)


def now():
    return time.time()
