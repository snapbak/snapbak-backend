"""Which tier polishes a chunk: the small model unless the chunk is hard (docs/escalation.md)."""

import re
from typing import Literal

type Tier = Literal["small", "big"]

CUES = [
    "no,",
    "no no",
    "no wait",
    "no actually",
    "wait",
    "scratch that",
    "i mean",
    "actually",
    "make that",
    "rather",
    "sorry",
    "change that",
    "nahi nahi",
    "nahin nahin",
    "nahi,",
    "nahin,",
    "matlab",
    "balki",
    "नहीं नहीं",
    "नहीं,",
    "मतलब",
    "बल्कि",
    "सॉरी",
    "आई मीन",
    "एक्चुअली",
    "वेट",
    "i meant",
    "oops",
    "nope",
    "let me rephrase",
    "mera matlab",
    "ruko",
    "galti se bola",
    "galti se",
    "मेरा मतलब",
    "रुको",
    "गलती से बोल दिया",
    "गलती से बोला",
    "गलती से",
    "make it",
    "correction",
    "strike that",
    "my bad",
    "ignore that",
    "cancel that",
    "no sorry",
    "galti ho gayi",
    "गलती हो गई",
]
BIG_LANGUAGES = {"hinglish", "hi"}
MAX_SMALL_WORDS = 150

LETTER = r"\wऀ-ॿ"
_alternatives = "|".join(re.escape(cue).replace(r"\ ", r"\s+") for cue in CUES)
CUE = re.compile(rf"(?<![{LETTER}])(?:{_alternatives})(?![{LETTER}])")


def has_cue(text: str) -> bool:
    found = CUE.search(text.lower())
    return found is not None


def tier(text: str, language: str) -> Tier:
    hard = has_cue(text) or language in BIG_LANGUAGES or len(text.split()) > MAX_SMALL_WORDS
    return "big" if hard else "small"


# The reasoning effort each route asks for: a chunk with a correction cue, Hindi, Hinglish, and the correction of a
# refused answer. Round b3 measured medium's gain on Hindi, Hinglish and cues (150 against 131 of 216 rows).
EFFORTS = {"cue": "medium", "hinglish": "medium", "hi": "medium", "correction": "medium"}
STRENGTH = ["", "minimal", "low", "medium", "high"]


def effort(text: str, language: str) -> str:
    """The effort a big-tier answer reasons at, "" for none: the higher of its routes' (a cue, Hindi or Hinglish); an
    English chunk that is big only for its length answers without it (founder, 13 Oct: faster)."""
    routes = [EFFORTS["cue"] if has_cue(text) else "", EFFORTS.get(language, "") if language in BIG_LANGUAGES else ""]
    return max(routes, key=STRENGTH.index)


# A paragraph said between two long paragraphs splits a chunk; each part is polished at once (founder, 13 Oct).
PARAGRAPH = re.compile(
    r"(?<![\wऀ-ॿ])(?:new paragraph|naya paragraph|naya para|नया पैराग्राफ|नया पैरा)(?![\wऀ-ॿ])", re.IGNORECASE
)
MIN_PARAGRAPH_WORDS = 30


def paragraphs(text: str) -> list[str]:
    """The chunk's paragraphs where one is said with at least 30 words on each side; else the chunk whole."""
    found = list(PARAGRAPH.finditer(text))
    cuts: list[re.Match[str]] = []
    since = 0
    for index, said in enumerate(found):
        upto = found[index + 1].start() if index + 1 < len(found) else len(text)
        before = len(text[since : said.start()].split())
        after = len(text[said.end() : upto].split())
        if before >= MIN_PARAGRAPH_WORDS and after >= MIN_PARAGRAPH_WORDS:
            cuts.append(said)
            since = said.end()
    starts = [0, *(said.end() for said in cuts)]
    ends = [*(said.start() for said in cuts), len(text)]
    parts = [text[start:end].strip() for start, end in zip(starts, ends, strict=True)]
    return parts if len(parts) > 1 else [text]
