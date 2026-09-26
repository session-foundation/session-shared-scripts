"""Text helpers with no dependency on any service.
"""
import re


# A long dash is the clearest tell that text was machine-written, and no reply this
# team has sent uses one. The prompts that write for customers forbid it; this is the
# failsafe, because a prompt rule is advisory and the text reaches a real person.
# The spaced form is punctuation and becomes a comma; anything left is joining two
# things, like a range, and becomes the hyphen a person would have typed. A dash that
# opens a line is a list bullet. Only spaces and tabs count, so line breaks survive.
LIST_DASH = re.compile(r"^([ \t]*)[—–](?=[ \t])", re.MULTILINE)
PUNCTUATING_DASH = re.compile(r"[ \t]+[—–][ \t]*|[ \t]*[—–][ \t]+")
ANY_LONG_DASH = re.compile(r"[—–]")


def _comma(match):
    return "," if match.string[match.end():match.end() + 1] in ("", "\n") else ", "


def undash_english(text):
    """Replace every em and en dash: punctuation with a comma, the rest with a hyphen.

    ENGLISH ONLY, and the name says so because passing anything else corrupts it. In
    Russian and the other East Slavic languages the long dash carries the present-tense
    copula that the grammar omits: "Москва — столица России" IS the verb, and the comma
    this produces leaves a subject with no predicate. Spanish, French, Polish and
    Chinese give it dialogue and parenthetical duty that a comma does not carry either.

    Only for text a model wrote. Rewriting punctuation somebody typed themselves would
    be wrong even in English.

    A failsafe, not a style pass: the substitution is blunt enough to turn a legitimate
    strong break into a comma splice ("I checked the logs — nothing was uploaded"), so
    the prompt is what should keep dashes out and this is what catches the misses.
    """
    text = LIST_DASH.sub(r"\1-", text or "")
    return ANY_LONG_DASH.sub("-", PUNCTUATING_DASH.sub(_comma, text))


def squash(value):
    """Collapse whitespace so subject/description can be compared meaningfully."""
    return re.sub(r"\s+", " ", value or "").strip()
