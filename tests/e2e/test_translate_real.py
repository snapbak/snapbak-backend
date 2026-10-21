"""TRX-07 on the real model (16-caption-translation LLD, Test plan, D19): a 10-minute talk's lines into Spanish and
Hindi keep their count and order. It calls the engine in SNAPBAK_TRANSLATE (else SNAPBAK_BIG) with the keys in the
environment, so it costs money: marked `cloud`, left out of `uv run pytest`, run by `just test-cloud` and charged to
the eval budget."""

import asyncio
import os
from uuid import uuid4

import pytest

from snapbak_backend.ai import languages
from snapbak_backend.ai.providers.factory import make_provider
from snapbak_backend.config.settings import KEYS
from snapbak_backend.schemas.translations import SourceLine
from snapbak_backend.services import translate

pytestmark = pytest.mark.cloud

# A product walkthrough's sentences, about 1,400 words over 150 lines as a 10-minute talk has.
SENTENCES = [
    "So here you can see the new dashboard we shipped last week.",
    "Every number on it updates live, so you never refresh the page.",
    "Let me open the settings and show you where the team lives.",
    "Press Command K to search for anything, a person or a project.",
    "Priya built this part, so I'll let her walk you through it.",
    "The chart on the left shows sign-ups, and the one on the right shows revenue.",
    "If you hover over a bar, you get the exact count for that day.",
    "We moved export to the top right, because people couldn't find it.",
    "Now, this is the part I'm most excited about.",
    "You can share a link, and it opens straight to the same view.",
]
LINES = 150


def talk() -> list[SourceLine]:
    page = [
        SourceLine(
            idx=idx,
            lineId=f"ln_{idx}",
            rev=0,
            sourceHash=f"h{idx}",
            text=f"{SENTENCES[idx % len(SENTENCES)]} That was point {idx + 1}.",
        )
        for idx in range(LINES)
    ]
    return page


async def translated(target: str) -> dict[int, str]:
    spec = os.environ.get("SNAPBAK_TRANSLATE") or os.environ["SNAPBAK_BIG"]
    keys = {provider: os.environ.get(name, "") for provider, name in KEYS.items()}
    chain = [make_provider(spec, keys)]
    page = talk()
    plan = translate.Plan(uuid4(), languages.BY_CODE[target], "en", "English", ["Priya"], page, page, {}, None)
    results = await asyncio.gather(
        *(translate.translate_batch(chain, plan, batch) for batch in translate.batches(page, page))
    )
    answers = {idx: text for good, _ in results for idx, text in good.items()}
    return answers


@pytest.mark.parametrize("target", ["es", "hi"])
def test_trx07_spanish_and_hindi_keep_line_count(target: str) -> None:
    answers = asyncio.run(translated(target))
    assert sorted(answers) == list(range(LINES)), "one translation per line, none merged, split or dropped"
    assert all(text.strip() and "\n" not in text for text in answers.values())
    if target == "hi":
        devanagari = [text for text in answers.values() if any("ऀ" <= c <= "ॿ" for c in text)]
        assert len(devanagari) == LINES, "Hindi in Devanagari"
