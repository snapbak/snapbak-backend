"""Hindi words of a transcript in Roman letters (TRX-09; docs/cloud.md): the `transliterate` route and task. Lines
with no Devanagari keep their words; the rest go to the polish model in batches. A line whose answer has another
word count keeps no Roman words, and the app shows its native words in Roman view (14 LLD, A8)."""

import json
import re
import time
from typing import Any
from uuid import UUID

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from snapbak_backend.ai.chain import Call, Tiers, ask
from snapbak_backend.db.models import TranscriptLine
from snapbak_backend.services import recordings

DEVANAGARI = re.compile("[ऀ-ॿ]")
BATCH_LINES = 40
DEADLINE_S = 60
SYSTEM = (
    "You write Hindi in everyday Roman letters, the way people type Hinglish (यहाँ is yahan, दिखता is dikhta, है is "
    "hai). The user sends JSON lines of words. Write each word in Roman letters; keep English words, numbers and "
    "punctuation as they are. Keep every line's words in the same order and the same count. Answer only JSON: "
    '{"lines": [{"i": <the line\'s i>, "words": ["..."]}]}.'
)


type Roman = tuple[str, list[dict[str, Any]]]


def romanized(line: TranscriptLine, words: list[str]) -> Roman | None:
    """The line's Roman text and words from the model's answer, each word keeping its times; None when the count
    differs."""
    if len(words) != len(line.words):
        return None
    roman_words = [{**word, "w": roman} for word, roman in zip(line.words, words, strict=True)]
    roman = (" ".join(words), roman_words)
    return roman


async def ask_batch(tiers: Tiers, batch: list[TranscriptLine]) -> dict[int, list[str]]:
    """The model's Roman words per line index; an answer it can't read gives none."""
    request = {"lines": [{"i": line.idx, "words": [word["w"] for word in line.words]} for line in batch]}
    turns = [("system", SYSTEM), ("user", json.dumps(request, ensure_ascii=False))]
    max_tokens = 4 * sum(len(line.text) for line in batch) + 200
    calls: list[Call] = []
    output = await ask(tiers.chain(tiers.polish), turns, max_tokens, time.monotonic() + DEADLINE_S, calls)
    try:
        answer: Any = json.loads(output)["lines"]
        words = {int(item["i"]): [str(word) for word in item["words"]] for item in answer}
    except ValueError, KeyError, TypeError:
        words = {}
    return words


async def keep(session: AsyncSession, made: list[tuple[TranscriptLine, Roman | None]]) -> None:
    """Writes Roman words to each line not edited (`rev` moved) or deleted since it was read, under the recording's
    lock, and commits."""
    if made:
        await recordings.lock(session, made[0][0].recording_id)
    for line, roman in made:
        if roman is None:
            continue
        text, words = roman
        await session.execute(
            update(TranscriptLine)
            .where(TranscriptLine.id == line.id, TranscriptLine.rev == line.rev, TranscriptLine.deleted_at.is_(None))
            .values(roman_text=text, roman_words=words)
            .execution_options(synchronize_session=False)
        )
    await session.commit()


async def transliterate(session: AsyncSession, tiers: Tiers, recording_id: UUID) -> list[TranscriptLine]:
    """Fills `roman_text` and `roman_words` of every live line that has none (an edit clears them) and returns the
    lines in order. The model may take minutes, so no transaction stays open while it thinks; each batch is kept as
    it arrives, so a failure later doesn't pay for it again. A line edited or deleted meanwhile keeps its change and
    gets no Roman words. Raises ProviderFailed when the model can't answer."""
    lines = await recordings.live_lines(session, recording_id)
    await session.commit()
    todo = [line for line in lines if line.roman_text is None]
    hindi = [line for line in todo if DEVANAGARI.search(line.text)]
    latin = [line for line in todo if not DEVANAGARI.search(line.text)]
    own = [(line, romanized(line, [word["w"] for word in line.words])) for line in latin]
    await keep(session, own)
    for first in range(0, len(hindi), BATCH_LINES):
        batch = hindi[first : first + BATCH_LINES]
        answers = await ask_batch(tiers, batch)
        await keep(session, [(line, romanized(line, answers.get(line.idx, []))) for line in batch])
    session.expire_all()
    fresh = await recordings.live_lines(session, recording_id)
    return fresh
