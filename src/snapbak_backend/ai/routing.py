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
    "i meant",
    "oops",
    "nope",
    "let me rephrase",
    "make it",
    "correction",
    "strike that",
    "my bad",
    "ignore that",
    "cancel that",
    "no sorry",
    "delete that",
    "no don't do this",
    "no don't do that",
]
MAX_SMALL_WORDS = 150

LETTER = r"\w"
_alternatives = "|".join(re.escape(cue).replace(r"\ ", r"\s+") for cue in CUES)
CUE = re.compile(rf"(?<![{LETTER}])(?:{_alternatives})(?![{LETTER}])")


def has_cue(text: str) -> bool:
    found = CUE.search(text.lower())
    return found is not None


def tier(text: str) -> Tier:
    hard = has_cue(text) or len(text.split()) > MAX_SMALL_WORDS
    return "big" if hard else "small"


# The reasoning effort each route asks for: a chunk with a correction cue, any other chunk, on either tier, and the
# correction of a refused answer. The 14 Oct 2026 English sweep kept every route at medium: none and low fell outside
# medium's noise band, and the models refuse "minimal" (docs/escalation.md).
EFFORTS = {"cue": "medium", "plain": "medium", "correction": "medium"}


def effort(text: str) -> str:
    """The effort an answer reasons at: its route's."""
    route = "cue" if has_cue(text) else "plain"
    return EFFORTS[route]


# A paragraph said between two long paragraphs splits a chunk; each part is polished at once (founder, 13 Oct).
PARAGRAPH = re.compile(r"(?<!\w)new paragraph(?!\w)", re.IGNORECASE)
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
