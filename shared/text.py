"""String helpers with no transport in them: prompts, ticket fields and Discord lines
all clip and compare text the same way."""
import re


def clip(text, limit):
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def squash(value):
    """Collapse whitespace so two renderings of the same prose compare equal."""
    return re.sub(r"\s+", " ", value or "").strip()


def window_label(hours):
    """`3 days` for a whole number of days, else `36h`."""
    if hours % 24 == 0 and hours >= 24:
        days = hours // 24
        return f"{days} day{'s' if days > 1 else ''}"
    return f"{hours}h"


# A long dash is the clearest tell that text was machine-written, and no reply this
# team has sent uses one. The prompts that write for customers forbid it; this is the
# failsafe, because a prompt rule is advisory and the text reaches a real person.
# The spaced form is punctuation and becomes a comma; anything left is joining two
# things, like a range, and becomes the hyphen a person would have typed.
PUNCTUATING_DASH = re.compile(r"(?:\s+[—–]\s*|\s*[—–]\s+)")


ANY_LONG_DASH = re.compile(r"[—–]")


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
    return ANY_LONG_DASH.sub("-", PUNCTUATING_DASH.sub(", ", text or ""))
