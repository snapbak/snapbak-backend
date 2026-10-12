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
