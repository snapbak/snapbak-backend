import pytest

from snapbak_backend.ai.chain import Tiers
from snapbak_backend.ai.providers.fake import Fake
from snapbak_backend.db.models import TranscriptLine
from snapbak_backend.services.transliteration import ask_batch, romanized


def line(idx: int, *words: str) -> TranscriptLine:
    made = [{"w": word, "s": n * 100, "e": n * 100 + 90, "filler": False, "conf": 0.9} for n, word in enumerate(words)]
    return TranscriptLine(idx=idx, text=" ".join(words), words=made)


@pytest.mark.anyio
async def test_trx09_every_line_of_a_batch_is_asked_and_answered() -> None:
    tiers = Tiers(polish=Fake("echo"), command=Fake("echo"), fallback=None)
    answers = await ask_batch(tiers, [line(3, "तो", "यहाँ"), line(7, "है")])
    assert answers == {3: ["r:तो", "r:यहाँ"], 7: ["r:है"]}


def test_trx09_roman_words_keep_each_words_times_and_a_wrong_count_keeps_none() -> None:
    made = line(0, "तो", "यहाँ")
    assert romanized(made, ["to"]) is None
    assert romanized(made, ["to", "yahan", "extra"]) is None
    assert romanized(made, ["to", "yahan"]) == (
        "to yahan",
        [
            {"w": "to", "s": 0, "e": 90, "filler": False, "conf": 0.9},
            {"w": "yahan", "s": 100, "e": 190, "filler": False, "conf": 0.9},
        ],
    )
