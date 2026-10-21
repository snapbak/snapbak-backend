"""Every fake-strategy fixture through the real merge, to its exact lines: the shapes the functional tests don't
reach (CJK word joining, cross-talk, one speaker without names)."""

import pytest

from snapbak_backend.transcription.fake import FakeTranscriptionStrategy
from snapbak_backend.transcription.merge import text


async def lines_of(fixture: str, language: str = "auto") -> list[tuple[str, str | None, str]]:
    async def enqueue(*_: object, **__: object) -> None:
        pass

    merged = await FakeTranscriptionStrategy(fixture, enqueue).result({"language": language})
    names = {speaker.key: speaker.name for speaker in merged.speakers}
    return [(line[0].speaker, names[line[0].speaker], text(line, merged.language)) for line in merged.lines]


@pytest.mark.anyio
async def test_trx01_japanese_words_join_without_spaces_into_one_sentence() -> None:
    assert await lines_of("japanese") == [("SPEAKER_00", None, "こんにちは、今日は新しい画面を見せます。")]


@pytest.mark.anyio
async def test_trx08_cross_talk_goes_to_the_larger_overlap_and_a_far_word_to_the_previous_speaker() -> None:
    assert await lines_of("overlap") == [
        ("SPEAKER_00", None, "I think we"),
        ("SPEAKER_01", None, "should wait, sorry."),
        ("SPEAKER_00", None, "ship it today."),
        ("SPEAKER_00", None, "Done."),
    ]


@pytest.mark.anyio
async def test_trx01_one_speaker_without_names() -> None:
    assert await lines_of("one-speaker-en") == [("SPEAKER_00", None, "This is a short demo of the new export screen.")]


@pytest.mark.anyio
async def test_trx01_hinglish_keeps_devanagari_and_latin_in_one_line() -> None:
    assert await lines_of("hinglish", "hinglish") == [
        ("SPEAKER_00", None, "मेरा रुकने का तो बहुत मन है, but I have an exam to give tomorrow.")
    ]
