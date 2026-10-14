"""The prompt and schema that put a ticket's conversation into English.

Shared by `claude: english` and the digest, which post the same private note.
"""
ENGLISH_TIMEOUT_SECONDS = 180


# A whole conversation rather than one description, so larger than the classifier's.
TRANSCRIPT_INPUT_CHARS = 8000


TRANSCRIPT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["turns"],
    "properties": {
        "turns": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["index", "english"],
                "properties": {
                    "index": {
                        "type": "integer",
                        "description": "The turn's index, echoed back unchanged.",
                    },
                    "english": {
                        "type": "string",
                        "description": "That turn in English, or the original text unchanged if it was already English.",
                    },
                },
            },
        }
    },
}

TRANSCRIPT_SYSTEM_PROMPT = (
    "You translate support conversations into English for an agent who does not read "
    "the original language.\n\n"
    "You are given the turns of one ticket as JSON, each with an index. Return one "
    "object per input turn, echoing its index back unchanged.\n\n"
    "Translate faithfully and completely. Keep the speaker's meaning, their order of "
    "events and their tone — an angry turn must still read as angry. Do not "
    "summarise, do not answer, do not merge turns, do not add notes of your own.\n\n"
    "A turn already in English is returned unchanged, word for word. Do not "
    "paraphrase it and do not 'improve' it.\n\n"
    "Leave Session IDs, version numbers, URLs and error strings exactly as written."
)


def is_english(finding):
    """Whether the classifier called this ticket English.

    Unknown counts as English: the note is only worth posting when it says
    something the agent cannot already read, and a blank `language` is far more
    likely to be a classification that came back thin than a ticket nobody could
    read. Guessing wrong this way costs a transcript nobody needed; the other way
    puts a machine translation over the top of words everyone could already read.
    """
    language = (finding.get("language") or "").strip().lower()
    return not language or language.startswith(("english", "en"))
