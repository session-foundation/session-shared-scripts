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
