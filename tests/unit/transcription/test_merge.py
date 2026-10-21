from typing import Any

from snapbak_backend.transcription.merge import Merged, SpeakerOut, Word, lines, merge, name_speakers, text
from snapbak_backend.worker import refusal


def aai_word(text: str, start: int, end: int, speaker: str = "A", confidence: float = 0.9) -> dict[str, Any]:
    return {"text": text, "start": start, "end": end, "confidence": confidence, "speaker": speaker}


def turn(speaker: str, start_s: float, end_s: float) -> dict[str, Any]:
    return {"speaker": speaker, "start": start_s, "end": end_s}


def word(w: str, s: int = 0, e: int = 1, speaker: str = "S0") -> Word:
    return Word(w=w, s=s, e=e, filler=False, conf=1.0, speaker=speaker)


def speakers_of(words: list[dict[str, Any]], turns: list[dict[str, Any]]) -> list[str]:
    merged = merge({"words": words, "language_code": "en", "audio_duration": 60}, turns, "auto")
    return [w.speaker for line in merged.lines for w in line]


def test_trx08_each_word_takes_the_turn_that_overlaps_it_most() -> None:
    words = [aai_word("one", 900, 1500), aai_word("two", 1500, 2600)]
    turns = [turn("SPEAKER_00", 0, 1.0), turn("SPEAKER_01", 1.0, 3.0)]
    assert speakers_of(words, turns) == ["SPEAKER_01", "SPEAKER_01"]


def test_trx08_a_tie_goes_to_the_earlier_turn() -> None:
    words = [aai_word("one", 900, 1100)]
    turns = [turn("SPEAKER_00", 0, 1.0), turn("SPEAKER_01", 1.0, 3.0)]
    assert speakers_of(words, turns) == ["SPEAKER_00"]


def test_trx08_a_word_in_a_gap_takes_the_nearest_turn_within_a_second() -> None:
    words = [aai_word("gap", 2300, 2500), aai_word("near", 3700, 3900)]
    turns = [turn("SPEAKER_00", 0, 2.0), turn("SPEAKER_01", 4.0, 5.0)]
    assert speakers_of(words, turns) == ["SPEAKER_00", "SPEAKER_01"]


def test_trx08_a_word_exactly_a_second_from_a_turn_still_takes_it() -> None:
    words = [aai_word("a", 1000, 1500), aai_word("edge", 6000, 6200)]
    turns = [turn("SPEAKER_00", 0, 2.0), turn("SPEAKER_01", 4.0, 5.0), turn("SPEAKER_00", 9.0, 10.0)]
    assert speakers_of(words, turns) == ["SPEAKER_00", "SPEAKER_01"]


def test_trx08_a_word_midway_between_two_turns_takes_the_earlier() -> None:
    words = [aai_word("mid", 2400, 2600)]
    turns = [turn("SPEAKER_00", 0, 2.0), turn("SPEAKER_01", 3.0, 4.0)]
    assert speakers_of(words, turns) == ["SPEAKER_00"]


def test_trx08_a_word_far_from_any_turn_keeps_the_previous_speaker() -> None:
    words = [aai_word("a", 4100, 4200), aai_word("far", 7000, 7200)]
    turns = [turn("SPEAKER_00", 0, 2.0), turn("SPEAKER_01", 4.0, 5.0), turn("SPEAKER_00", 9.0, 10.0)]
    assert speakers_of(words, turns) == ["SPEAKER_01", "SPEAKER_01"]


def test_trx08_no_turns_at_all_means_one_speaker() -> None:
    words = [aai_word("a.", 0, 100), aai_word("b", 200, 300)]
    assert speakers_of(words, []) == ["SPEAKER_00", "SPEAKER_00"]


def test_trx08_the_sweep_keeps_times_exactly_as_assemblyai_gave_them() -> None:
    words = [aai_word("Hi.", 1203, 1351, confidence=0.98)]
    merged = merge({"words": words, "language_code": "en_us", "audio_duration": 2}, [turn("S", 1.2, 1.4)], "auto")
    assert merged.lines == [[Word(w="Hi.", s=1203, e=1351, filler=False, conf=0.98, speaker="S")]]
    assert (merged.language, merged.audio_ms) == ("en_us", 2000)


def test_trx08_names_map_by_overlap_and_a_contested_speaker_keeps_the_larger_name() -> None:
    # The smaller claim comes first, so a "first name wins" bug fails.
    words = [
        aai_word("Hello", 0, 500, "Arjun"),
        aai_word("I'm", 1000, 2000, "Priya"),
        aai_word("Priya.", 2000, 3000, "Priya"),
        aai_word("there.", 3500, 3600, "B"),
    ]
    assigned = ["SPEAKER_00", "SPEAKER_00", "SPEAKER_00", "SPEAKER_01"]
    assert name_speakers(words, assigned) == {"SPEAKER_00": "Priya"}


def test_trx08_a_name_holding_exactly_half_its_time_on_one_speaker_is_kept() -> None:
    words = [aai_word("a", 0, 100, "Priya"), aai_word("b", 100, 200, "Priya")]
    assert name_speakers(words, ["S0", "S1"]) == {"S0": "Priya"}


def test_trx08_a_name_spread_thin_over_speakers_is_dropped() -> None:
    words = [aai_word("a", 0, 100, "Priya"), aai_word("b", 100, 200, "Priya"), aai_word("c", 200, 300, "Priya")]
    assigned = ["S0", "S1", "S2"]
    assert name_speakers(words, assigned) == {}


def test_trx01_lines_split_at_sentence_ends_in_latin_devanagari_and_cjk() -> None:
    words = [word("Hi."), word("नमस्ते।"), word("こんにちは。"), word("好吗\uff1f"), word("Ok?"), word("tail")]
    split = [[w.w for w in line] for line in lines(words)]
    assert split == [["Hi."], ["नमस्ते।"], ["こんにちは。"], ["好吗\uff1f"], ["Ok?"], ["tail"]]


def test_trx08_lines_split_when_the_speaker_changes() -> None:
    words = [word("so", speaker="S0"), word("yes", speaker="S1"), word("right.", speaker="S1")]
    assert [[w.w for w in line] for line in lines(words)] == [["so"], ["yes", "right."]]


def test_trx01_lines_split_at_30_words_or_a_comma_after_20() -> None:
    long = [word(f"w{n}") for n in range(31)]
    assert [len(line) for line in lines(long)] == [30, 1]
    soft = [word(f"w{n}") for n in range(19)] + [word("early,")] + [word(f"x{n}") for n in range(3)]
    assert [len(line) for line in lines(soft)] == [20, 3]
    before = [word("early,")] + [word(f"w{n}") for n in range(5)]
    assert [len(line) for line in lines(before)] == [6]
    nineteenth = [word(f"w{n}") for n in range(18)] + [word("nineteen,")] + [word(f"x{n}") for n in range(3)]
    assert [len(line) for line in lines(nineteenth)] == [22]


def test_trx01_text_joins_with_spaces_except_japanese_and_chinese() -> None:
    assert text([word("So"), word("here.")], "en") == "So here."
    assert text([word("こんにちは"), word("世界。")], "ja") == "こんにちは世界。"
    assert text([word("你好"), word("。")], "zh") == "你好。"


def test_trx13_english_fillers_are_tagged_and_only_in_english() -> None:
    words = [aai_word("Um,", 0, 100), aai_word("so", 100, 200), aai_word("uh.", 200, 300)]
    english = merge({"words": words, "language_code": "en_us", "audio_duration": 1}, [], "auto")
    hindi = merge({"words": words, "language_code": "hi", "audio_duration": 1}, [], "auto")
    hinglish = merge({"words": words, "language_code": "en", "audio_duration": 1}, [], "hinglish")
    assert [w.filler for line in english.lines for w in line] == [True, False, True]
    assert [w.filler for line in hindi.lines for w in line] == [False, False, False]
    assert [w.filler for line in hinglish.lines for w in line] == [False, False, False]


def test_trx08_speakers_come_in_order_of_first_word_with_their_names() -> None:
    words = [aai_word("Hi", 0, 500, "B"), aai_word("Priya.", 500, 1000, "B"), aai_word("Yes.", 2000, 2500, "Priya")]
    turns = [turn("SPEAKER_01", 0, 1.0), turn("SPEAKER_00", 2.0, 3.0)]
    merged = merge({"words": words, "language_code": "en", "audio_duration": 3}, turns, "auto")
    assert merged.speakers == [SpeakerOut("SPEAKER_01", None), SpeakerOut("SPEAKER_00", "Priya")]


def test_trx01_no_words_gives_no_lines() -> None:
    merged = merge({"words": [], "language_code": "en", "audio_duration": 5}, [], "auto")
    assert (merged.lines, merged.speakers) == ([], [])


def finished(language: str = "en", audio_ms: int = 60_000, words: int = 1) -> Merged:
    said = [[word("Hi.")]] if words else []
    return Merged(language=language, audio_ms=audio_ms, speakers=[], lines=said)


def test_cld03_a_finished_transcript_is_refused_over_2_hours_and_a_minute_by_its_own_duration() -> None:
    assert refusal(finished(audio_ms=7_260_000)) is None
    assert refusal(finished(audio_ms=7_261_000)) == "too_long"


def test_trx01_a_finished_transcript_in_another_language_or_with_no_words_is_refused() -> None:
    assert refusal(finished(language="en_us")) is None
    assert refusal(finished(language="ta")) == "unsupported_language"
    assert refusal(finished(words=0)) == "no_speech"
