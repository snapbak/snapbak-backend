"""What a chunk holds: a correction cue, and the paragraphs said in it (docs/escalation.md)."""

import json
import re
from pathlib import Path
from typing import Literal

# An answer's tier in the response: "small" when the polish model answered, "big" when a correction did.
type Tier = Literal["small", "big"]

# The correction cues, shared with the app: it embeds a byte-identical copy of this file (15 Oct 2026).
CUES: list[str] = json.loads((Path(__file__).parent / "cues.json").read_text(encoding="utf-8"))

LETTER = r"\w"
_alternatives = "|".join(re.escape(cue).replace(r"\ ", r"\s+") for cue in CUES)
CUE = re.compile(rf"(?<![{LETTER}])(?:{_alternatives})(?![{LETTER}])")


def has_cue(text: str) -> bool:
    found = CUE.search(text.lower())
    return found is not None


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
