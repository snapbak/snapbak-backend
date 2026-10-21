"""The transcript eval's metrics on hand-worked examples, and the live tier's guard rails with fakes (13 LLD, Evals).
No test here downloads anything or calls a vendor."""

import asyncio
import json
import random
import zipfile
from collections.abc import Awaitable, Callable
from difflib import SequenceMatcher
from itertools import pairwise
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import eval_transcript as ev
import pytest

from snapbak_backend.transcription.merge import Word


def word(w: str, s: int, e: int, speaker: str = "A", filler: bool = False) -> Word:
    return Word(w=w, s=s, e=e, filler=filler, conf=1.0, speaker=speaker)


def test_trx01_the_best_mapping_beats_the_greedy_one() -> None:
    gain = {("X", "A"): 3.0, ("X", "B"): 2.0, ("Y", "A"): 2.5, ("Y", "B"): 0.0}
    assert ev.best_mapping(gain) == {"X": "B", "Y": "A"}


def test_trx01_an_extra_hypothesis_speaker_stays_unmapped() -> None:
    gain = {("X", "A"): 5.0, ("Y", "A"): 1.0}
    assert ev.best_mapping(gain) == {"X": "A"}


def test_trx01_der_counts_the_half_second_given_to_the_wrong_speaker() -> None:
    ref = [("A", 0, 1000), ("B", 1000, 2000)]
    hyp = [("X", 0, 1500), ("Y", 1500, 2000)]
    assert ev.der(ref, hyp) == 25.0


def test_trx01_der_counts_missed_and_false_alarm_time() -> None:
    ref = [("A", 0, 1000)]
    hyp = [("X", 500, 1500)]
    # 500 ms missed and 500 ms false alarm over 1,000 ms of speech.
    assert ev.der(ref, hyp) == 100.0


def test_trx01_words_align_on_their_letters_ignoring_case_and_marks() -> None:
    truth = [word("Hi,", 0, 100), word("I'm", 100, 200), word("David.", 200, 300)]
    hyp = [word("hi", 0, 100), word("um", 100, 150), word("david", 200, 300)]
    assert ev.align(truth, hyp) == [(0, 0), (2, 2)]


def test_trx01_speaker_word_accuracy_maps_speakers_by_shared_words() -> None:
    truth = [word("a", 0, 1, "A"), word("b", 1, 2, "A"), word("c", 2, 3, "B"), word("d", 3, 4, "B")]
    hyp = [word("a", 0, 1, "X"), word("b", 1, 2, "X"), word("c", 2, 3, "X"), word("d", 3, 4, "Y")]
    pairs = ev.align(truth, hyp)
    assert ev.speaker_words(truth, hyp, pairs) == 75.0


def test_trx01_a_missed_word_counts_against_speaker_word_accuracy() -> None:
    truth = [word("a", 0, 1, "A"), word("b", 1, 2, "A")]
    hyp = [word("a", 0, 1, "X")]
    assert ev.speaker_words(truth, hyp, ev.align(truth, hyp)) == 50.0


def test_trx01_boundary_errors_pool_starts_and_ends() -> None:
    truth = [word("a", 0, 100), word("b", 200, 300)]
    hyp = [word("a", 10, 130), word("b", 250, 300)]
    # Errors 10, 30, 50, 0: median 20, p95 the largest.
    assert ev.boundary_errors(truth, hyp, ev.align(truth, hyp)) == (20.0, 50.0)


def test_trx01_line_breaks_score_precision_and_recall_of_the_breaks() -> None:
    truth = [word("a", 0, 1), word("b.", 1, 2), word("c", 2, 3), word("d.", 3, 4), word("e", 4, 5), word("f.", 5, 6)]
    hyp = list(truth)
    # Truth breaks after b and d; the hypothesis breaks after a and d.
    hyp_lines = [[hyp[0]], hyp[1:4], hyp[4:]]
    assert ev.line_breaks(truth, hyp, hyp_lines, ev.align(truth, hyp)) == 50.0


def test_trx01_a_line_holding_two_true_speakers_is_mixed() -> None:
    truth = [word("a", 0, 1, "A"), word("b", 1, 2, "B"), word("c", 2, 3, "B")]
    hyp = [word("a", 0, 1, "X"), word("b", 1, 2, "X"), word("c", 2, 3, "Y")]
    hyp_lines = [hyp[:2], hyp[2:]]
    assert ev.mixed_lines(truth, hyp, hyp_lines, ev.align(truth, hyp)) == 50.0


def test_trx04_fillers_score_precision_and_recall() -> None:
    truth = [word("um", 0, 1, filler=True), word("so", 1, 2), word("uh", 2, 3, filler=True), word("ok", 3, 4)]
    hyp = [word("um", 0, 1, filler=True), word("so", 1, 2, filler=True), word("uh", 2, 3), word("ok", 3, 4)]
    assert ev.filler_scores(truth, hyp, ev.align(truth, hyp)) == (50.0, 50.0)


def test_trx04_no_fillers_anywhere_scores_full_marks() -> None:
    truth = [word("so", 0, 1)]
    assert ev.filler_scores(truth, truth, ev.align(truth, truth)) == (100.0, 100.0)


def test_trx04_a_second_of_silence_is_a_true_pause_and_a_shorter_one_is_not() -> None:
    words = [word("a", 0, 500), word("b", 1500, 2000), word("c", 2900, 3000)]
    assert ev.silences(words) == [(500, 1500)]


def test_trx04_a_silence_next_to_a_filler_is_not_a_true_pause() -> None:
    words = [word("a", 0, 500), word("um", 1600, 1800, filler=True), word("b", 3000, 3200)]
    assert ev.silences(words) == []


def test_trx04_a_silence_needs_everyone_quiet_so_overlapping_speech_is_no_gap() -> None:
    words = [word("long", 0, 5000, "A"), word("a", 100, 200, "B"), word("b", 4000, 4500, "B")]
    assert ev.silences(words) == []


def test_trx04_pause_scores_match_overlapping_pauses_and_count_cuts_into_speech() -> None:
    truth = [word("a", 0, 500), word("b", 2000, 2500), word("c", 2600, 2700), word("d", 4000, 4200)]
    # The hypothesis misses "c", so it offers 2500-4000, whose cut 2650-3850 clips "c"; it misses no pause.
    hyp = [word("a", 0, 500), word("b", 2000, 2500), word("d", 4000, 4200)]
    assert ev.pause_scores(truth, hyp) == (100.0, 50.0)


def test_trx04_a_pause_cut_keeps_150_ms_each_side_so_a_slightly_late_edge_clips_nothing() -> None:
    truth = [word("one", 0, 500), word("two", 1600, 2000)]
    # Heard with the gap a little wide: the offered pause is 450 to 1650, but its cut is 600 to 1500.
    heard = [word("one", 0, 450), word("two", 1650, 2000)]
    assert ev.pause_scores(truth, heard)[1] == 0.0
    # Heard far too wide: the cut, 450 to 1650, now cuts into both true words.
    far = [word("one", 0, 300), word("two", 1800, 2000)]
    assert ev.pause_scores(truth, far)[1] == 100.0


def test_trx02_captions_break_on_speaker_pause_sentence_length_and_rows() -> None:
    words = [
        word("a", 0, 100, "A"),
        word("b", 100, 200, "B"),  # another speaker
        word("c", 900, 1000, "B"),  # 700 ms after b
        word("d.", 1000, 1100, "B"),
        word("e", 1100, 1200, "B"),  # after a sentence end
        word("um", 1200, 1300, "B", filler=True),  # skipped
    ]
    groups = [[item.w for item in caption] for caption in ev.captions(words)]
    assert groups == [["a"], ["b"], ["c", "d."], ["e"]]


def test_trx02_a_caption_holds_at_most_two_rows_of_seven_words() -> None:
    words = [word("w", n * 100, n * 100 + 50) for n in range(15)]
    sizes = [len(caption) for caption in ev.captions(words)]
    assert sizes == [14, 1]


def test_trx02_a_caption_lasts_at_most_seven_seconds() -> None:
    words = [word("w", 0, 100), word("w", 600, 7200)]
    sizes = [len(caption) for caption in ev.captions(words)]
    assert sizes == [1, 1]


def test_trx02_caption_violations_count_mixed_speakers_and_skipped_pauses() -> None:
    truth = [word("a", 0, 100, "A"), word("b", 100, 200, "B"), word("c", 1000, 1100, "B"), word("d", 1100, 1200, "B")]
    # One caption: the hypothesis heard a and b as one speaker; c and d as another, with no gap.
    hyp = [word("a", 0, 100, "X"), word("b", 100, 200, "X"), word("c", 300, 400, "Y"), word("d", 400, 500, "Y")]
    hyp_captions = [hyp[:2], hyp[2:]]
    assert ev.caption_violations(truth, hyp, hyp_captions, ev.align(truth, hyp)) == 50.0
    # Truth has a 800 ms pause between b and c that one caption runs across.
    assert ev.caption_violations(truth, hyp, [hyp[1:]], ev.align(truth, hyp)) == 100.0


def test_trx01_vendor_inputs_are_the_same_for_the_same_seed() -> None:
    truth = [word(f"w{n}.", n * 400, n * 400 + 300, "AB"[n // 10 % 2]) for n in range(100)]
    first = ev.vendor_inputs(truth, "seed")
    assert first == ev.vendor_inputs(truth, "seed")
    assert first != ev.vendor_inputs(truth, "other")
    transcript, output = first
    starts = [item["start"] for item in transcript["words"]]
    assert starts == sorted(starts)
    assert all(item["start"] < item["end"] for item in transcript["words"])
    assert {turn["speaker"] for turn in output["exclusiveDiarization"]} == {"SPEAKER_00", "SPEAKER_01"}


def test_trx01_the_fake_assemblyai_words_never_overlap_or_reorder_like_the_real_single_stream() -> None:
    truth = [word("imagine", 0, 1000, "A"), word("I", 10, 20, "B"), word("so", 990, 1200, "A")]
    transcript, _ = ev.vendor_inputs(truth, None)
    times = [(item["text"], item["start"], item["end"]) for item in transcript["words"]]
    assert times == [("imagine", 0, 1000), ("I", 1000, 1001), ("so", 1001, 1200)]
    noisy = [word(f"w{n}", n * 50, n * 50 + 50, "AB"[n % 2]) for n in range(400)]
    heard = ev.vendor_inputs(noisy, "seed")[0]["words"]
    assert all(b["start"] >= a["end"] for a, b in pairwise(heard))


def test_trx01_exclusive_turns_give_overlap_to_the_speaker_who_started_last() -> None:
    truth = [word("a", 0, 2000, "A"), word("b", 1000, 1500, "B")]
    assert ev.exclusive_turns(truth) == [("A", 0, 1000), ("B", 1000, 1500), ("A", 1500, 2000)]


def test_trx01_cp_wer_pairs_swapped_speakers_and_counts_each_error_over_the_true_words() -> None:
    truth = [word(w, n * 100, n * 100 + 50, "A") for n, w in enumerate("a b c d".split())]
    truth += [word(w, 1000 + n * 100, 1050 + n * 100, "B") for n, w in enumerate("e f".split())]
    # The vendor calls the speakers the other way round (A as "Y", B as "X") and mishears one word: one error in six.
    swapped = [word(w, n * 100, n * 100 + 50, "Y") for n, w in enumerate("a b x d".split())]
    swapped += [word(w, 1000 + n * 100, 1050 + n * 100, "X") for n, w in enumerate("e f".split())]
    assert round(ev.cp_wer(truth, swapped), 2) == 16.67
    # One speaker for both: the other speaker's two words are missed and the two extra words are added.
    merged = [word(w, n * 100, n * 100 + 50, "X") for n, w in enumerate("a b c d e f".split())]
    assert round(ev.cp_wer(truth, merged), 2) == 66.67


def test_trx01_a_worse_metric_is_a_regression_and_a_better_one_is_not() -> None:
    baseline = {"all": {"merge DER %": 10.0, "speaker words %": 90.0}}
    worse = {"all": {"merge DER %": 11.0, "speaker words %": 95.0}}
    better = {"all": {"merge DER %": 9.0, "speaker words %": 90.0}, "new": {"merge DER %": 50.0}}
    assert ev.regressions(baseline, worse) == ["all: merge DER % 10.00 -> 11.00"]
    assert ev.regressions(baseline, better) == []


def test_trx01_a_row_missing_from_a_run_is_a_regression() -> None:
    assert ev.regressions({"gone": {"merge DER %": 1.0}}, {}) == ["gone: missing"]


def test_trx01_a_metric_missing_from_a_row_is_a_regression() -> None:
    assert ev.regressions({"a": {"merge DER %": 1.0}}, {"a": {}}) == ["a: merge DER % missing"]


def test_trx01_scores_keep_two_decimals() -> None:
    truth = [word("a", 0, 100), word("b", 200, 300), word("c", 400, 500)]
    transcript, output = ev.vendor_inputs(truth, None)
    transcript["words"] = transcript["words"][:1]  # the vendor hears one word in three
    assert ev.score(truth, (transcript, output))["speaker words %"] == 33.33


# The live tier's guard rails. Nothing here calls a vendor: the transcriber is a fake.


def test_trx01_live_refuses_to_start_without_a_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ASSEMBLYAI_API_KEY", "x")
    monkeypatch.setenv("PYANNOTE_API_KEY", "x")
    with pytest.raises(SystemExit, match="--budget-usd"):
        ev.main(["--live"])


@pytest.mark.parametrize("missing", ["ASSEMBLYAI_API_KEY", "PYANNOTE_API_KEY"])
def test_trx01_live_refuses_to_start_without_both_keys(monkeypatch: pytest.MonkeyPatch, missing: str) -> None:
    monkeypatch.setenv("ASSEMBLYAI_API_KEY", "x")
    monkeypatch.setenv("PYANNOTE_API_KEY", "x")
    monkeypatch.delenv(missing)
    with pytest.raises(SystemExit, match=missing):
        ev.main(["--live", "--budget-usd", "1"])


def excerpts(count: int) -> list[ev.Excerpt]:
    truth = [word("Hello.", 0, 500, "A"), word("Hi.", 1000, 1400, "B")]
    made = [ev.Excerpt(f"T{n}-0", 0, 600_000, truth, Path(f"/nowhere/{n}.wav")) for n in range(count)]
    return made


class Heard:
    """A fake transcriber that hears the truth exactly in 30 s."""

    def __init__(self) -> None:
        self.asked: list[str] = []

    async def __call__(self, excerpt: ev.Excerpt) -> tuple[float, dict, dict]:
        self.asked.append(excerpt.name)
        transcript, output = ev.vendor_inputs(excerpt.truth, None)
        return 30.0, transcript, output


def test_trx01_live_stops_before_the_excerpt_that_would_pass_the_budget(tmp_path: Path) -> None:
    ledger = tmp_path / "spend.jsonl"
    ledger.write_text(json.dumps({"usd": 0.5, "items": 1, "spec": "earlier", "note": "ok"}) + "\n")
    heard = Heard()
    # 10 minutes cost $0.37 / 6; the ledger's $0.50 plus one excerpt fits under $0.60, two don't.
    rows = asyncio.run(ev.live(excerpts(3), heard, ledger, 0.6, tmp_path / "recorded"))
    assert heard.asked == ["T0-0"]
    assert list(rows) == ["T0-0 live"]
    entry = json.loads(ledger.read_text().splitlines()[1])
    assert entry["usd"] == pytest.approx(0.37 / 6)
    assert (entry["items"], entry["spec"], entry["note"]) == (1, "eval-transcript-live", "budget reached")


def test_trx01_live_saves_the_vendor_answers_for_the_replay_tier(tmp_path: Path) -> None:
    heard = Heard()
    rows = asyncio.run(ev.live(excerpts(1), heard, tmp_path / "spend.jsonl", 5.0, tmp_path / "recorded"))
    saved = tmp_path / "recorded" / "T0-0"
    assert json.loads((saved / "assemblyai.json").read_text())["words"][0]["text"] == "Hello."
    assert "exclusiveDiarization" in json.loads((saved / "pyannote.json").read_text())
    scored = rows["T0-0 live"]
    entry = json.loads((tmp_path / "spend.jsonl").read_text())
    assert (entry["items"], entry["spec"], entry["note"]) == (1, "eval-transcript-live", "ok")
    assert (scored["WER %"], scored["cpWER %"], scored["speaker words %"]) == (0.0, 0.0, 100.0)
    assert (scored["s per 10 min"], scored["$ per audio hour"]) == (30.0, 0.37)


# `run_live` end to end with the vendors, the bucket and the audio faked out.


class Vendors:
    """Stands in for the bucket client, the strategy and the audio cut, and records what was closed."""

    closed = False

    def __init__(self, *_: object) -> None: ...

    async def aclose(self) -> None:
        Vendors.closed = True


@pytest.fixture
def live_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A live run with both keys, a cloud config, two excerpts and no network."""
    monkeypatch.setenv("ASSEMBLYAI_API_KEY", "x")
    monkeypatch.setenv("PYANNOTE_API_KEY", "x")
    cloud = SimpleNamespace(assemblyai_key="a", pyannote_key="p", bucket="bucket")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"excerpts": [{"meeting": "T0"}, {"meeting": "T1"}]}))
    monkeypatch.setattr(ev, "MANIFEST", manifest)
    monkeypatch.setattr(ev, "OUT", tmp_path / "results.json")
    monkeypatch.setattr(ev, "load", lambda _: SimpleNamespace(cloud=cloud))
    monkeypatch.setattr(ev, "excerpts_of", lambda _: excerpts(2))
    monkeypatch.setattr(ev, "fetch", lambda _: Path("/nowhere"))
    monkeypatch.setattr(ev, "s3_client", lambda _: None)
    monkeypatch.setattr(ev, "Storage", Vendors)
    monkeypatch.setattr(ev, "AssemblyAiPyannoteStrategy", Vendors)
    Vendors.closed = False
    return tmp_path


def fake_live(rows: ev.Rows) -> Callable[..., Awaitable[ev.Rows]]:
    async def live(*_: object) -> ev.Rows:
        return rows

    return live


def test_trx01_live_needs_the_cloud_settings_for_the_bucket(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("ASSEMBLYAI_API_KEY", "PYANNOTE_API_KEY", "SNAPBAK_TOKEN", "SNAPBAK_BIG"):
        monkeypatch.setenv(name, "x")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(SystemExit, match="DATABASE_URL"):
        ev.main(["--live", "--budget-usd", "1"])


def test_trx01_a_live_run_over_the_time_cap_fails_and_names_the_excerpt(
    live_run: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    rows = {"T0-0 live": {"s per 10 min": 121.0}, "T1-0 live": {"s per 10 min": 30.0}}
    monkeypatch.setattr(ev, "live", fake_live(rows))
    assert ev.main(["--live", "--budget-usd", "5", "--ledger", str(live_run / "spend.jsonl")]) == 1
    assert "over the 120 s cap: T0-0 live" in capsys.readouterr().out
    assert Vendors.closed
    assert json.loads((live_run / "eval-transcript-live.json").read_text()) == rows


def test_trx01_a_live_run_that_finishes_within_the_cap_passes(
    live_run: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    rows = {"T0-0 live": {"s per 10 min": 120.0}, "T1-0 live": {"s per 10 min": 30.0}}
    monkeypatch.setattr(ev, "live", fake_live(rows))
    assert ev.main(["--live", "--budget-usd", "5", "--ledger", str(live_run / "spend.jsonl")]) == 0
    assert "cap" not in capsys.readouterr().out


def test_trx01_a_live_run_the_budget_cut_short_fails(
    live_run: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(ev, "live", fake_live({"T0-0 live": {"s per 10 min": 30.0}}))
    assert ev.main(["--live", "--budget-usd", "5", "--ledger", str(live_run / "spend.jsonl")]) == 1
    assert "budget reached after 1 of 2 excerpts" in capsys.readouterr().out


# What the replay tier's helpers do at their edges.


def test_trx01_one_speakers_words_half_a_second_apart_are_two_turns_and_closer_ones_one() -> None:
    apart = [word("a", 0, 100), word("b", 600, 700)]
    assert ev.speaker_turns(apart) == [("A", 0, 100), ("A", 600, 700)]
    close = [word("a", 0, 100), word("b", 599, 700)]
    assert ev.speaker_turns(close) == [("A", 0, 700)]


def test_trx01_exclusive_turns_of_a_speaker_with_a_gap_stay_two_runs() -> None:
    words = [word("a", 0, 100), word("b", 700, 800)]
    assert ev.exclusive_turns(words) == [("A", 0, 100), ("A", 700, 800)]


def test_trx01_spans_that_only_touch_do_not_overlap() -> None:
    assert not ev.overlaps(0, 10, [(10, 20)])
    assert not ev.overlaps(10, 20, [(0, 10)])
    assert ev.overlaps(0, 11, [(10, 20)])
    assert ev.overlaps(9, 20, [(0, 10)])


def test_trx01_a_long_talk_with_common_words_aligns_as_a_full_match_would() -> None:
    # Over 200 words, difflib's default sets aside the common ones and loses matches; ours must not.
    rng = random.Random(3)
    vocabulary = ["the", "a", "and", "to", *[f"w{n}" for n in range(30)]]
    said = [rng.choice(vocabulary) for _ in range(225)]
    heard = said[:]
    for _ in range(10):
        heard[rng.randrange(len(heard))] = rng.choice(vocabulary)
    truth = [word(text, n * 10, n * 10 + 5) for n, text in enumerate(said)]
    hyp = [word(text, n * 10, n * 10 + 5) for n, text in enumerate(heard)]
    full = sum(block.size for block in SequenceMatcher(None, said, heard, autojunk=False).get_matching_blocks())
    junked = sum(block.size for block in SequenceMatcher(None, said, heard).get_matching_blocks())
    assert junked < full, "the premise: difflib's default loses matches on this talk"
    assert len(ev.align(truth, hyp)) == full


def test_trx01_the_95th_percentile_of_word_boundary_errors_is_the_95th() -> None:
    truth = [word("w", n * 100, n * 100 + 10) for n in range(40)]
    # Each start is off by n ms and each end exactly right: errors 0 to 39 and forty zeros.
    heard = [word("w", n * 100 + n, n * 100 + 10) for n in range(40)]
    pairs = [(n, n) for n in range(40)]
    median, p95 = ev.boundary_errors(truth, heard, pairs)
    assert (median, p95) == (0.0, 35.0)


def test_trx01_ami_hmm_is_a_filler(tmp_path: Path) -> None:
    archive = tmp_path / "hmm.zip"
    said = '<root><w starttime="1.00" endtime="1.30">Hmm</w><w starttime="2.00" endtime="2.30">Yes</w></root>'
    with zipfile.ZipFile(archive, "w") as made:
        made.writestr("words/T1.A.words.xml", said)
    truth = ev.ami_words(archive, "T1", 0, 10)
    assert [(item.w, item.filler) for item in truth] == [("Hmm", True), ("Yes", False)]


def test_trx01_a_one_stream_replay_keeps_a_short_turn_the_seeded_one_may_drop() -> None:
    truth = [word("quick", 0, 100)]
    _, unseeded = ev.vendor_inputs(truth, None)
    assert len(unseeded["exclusiveDiarization"]) == 1


def test_trx01_the_all_row_is_each_metric_averaged_over_the_excerpts() -> None:
    rows = {"x": {"merge DER %": 10.0}, "y": {"merge DER %": 30.0}}
    assert ev.averaged(rows) == {"merge DER %": 20.0}


def test_trx01_wer_in_the_live_row_is_a_percentage() -> None:
    truth = [word("one", 0, 100, "A"), word("two", 200, 300, "A")]
    transcript, output = ev.vendor_inputs(truth, None)
    transcript["words"][1]["text"] = "to"
    row = ev.live_score(ev.Excerpt("T0-0", 0, 600_000, truth, Path("/nowhere")), 30.0, (transcript, output))
    assert row["WER %"] == 50.0


def test_trx01_live_needs_both_vendor_keys_in_the_cloud_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("ASSEMBLYAI_API_KEY", "PYANNOTE_API_KEY"):
        monkeypatch.setenv(name, "x")
    cloud = SimpleNamespace(assemblyai_key="", pyannote_key="p", bucket="bucket")
    monkeypatch.setattr(ev, "load", lambda _: SimpleNamespace(cloud=cloud))
    with pytest.raises(SystemExit, match="DATABASE_URL"):
        ev.main(["--live", "--budget-usd", "1"])


def test_trx01_a_live_run_fetches_every_excerpts_audio_first(live_run: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fetched: list[dict] = []
    monkeypatch.setattr(ev, "fetch", lambda entry: fetched.append(entry) or Path("/nowhere"))
    monkeypatch.setattr(ev, "live", fake_live({}))
    ev.main(["--live", "--budget-usd", "5", "--ledger", str(live_run / "spend.jsonl")])
    assert fetched == [{"meeting": "T0"}, {"meeting": "T1"}]


# Round 5: the metrics' bases and the helpers' edges, with literal values worked by hand.


def zipped(tmp_path: Path, xml: str) -> Path:
    archive = tmp_path / "edge.zip"
    with zipfile.ZipFile(archive, "w") as made:
        made.writestr("words/T1.A.words.xml", xml)
    return archive


def test_trx01_a_mark_after_an_untimed_word_joins_nothing(tmp_path: Path) -> None:
    said = (
        '<root><w starttime="1.00" endtime="1.50">Hi</w><w>untimed</w><w punc="true">!</w>'
        '<w starttime="2.00" endtime="2.50">there</w></root>'
    )
    truth = ev.ami_words(zipped(tmp_path, said), "T1", 0, 10)
    assert [item.w for item in truth] == ["Hi", "there"]


def test_trx01_words_starting_together_come_in_the_order_they_end(tmp_path: Path) -> None:
    said = '<root><w starttime="1.00" endtime="3.00">long</w><w starttime="1.00" endtime="2.00">short</w></root>'
    truth = ev.ami_words(zipped(tmp_path, said), "T1", 0, 10)
    assert [item.w for item in truth] == ["short", "long"]
    later = '<root><w starttime="1.00" endtime="5.00">first</w><w starttime="2.00" endtime="3.00">second</w></root>'
    assert [item.w for item in ev.ami_words(zipped(tmp_path, later), "T1", 0, 10)] == ["first", "second"]


def test_trx01_a_word_said_inside_a_longer_one_does_not_shorten_the_turn() -> None:
    inside = [word("long", 0, 1000), word("in", 100, 200)]
    assert ev.speaker_turns(inside) == [("A", 0, 1000)]


def test_trx01_the_der_denominator_counts_each_speaker_of_overlapping_speech() -> None:
    both = [("A", 0, 100), ("B", 0, 100)]
    assert ev.der(both, [("A", 0, 100)]) == 50.0


def test_trx01_merge_der_is_the_extra_heard_time_over_the_true_time() -> None:
    truth = [word("a", 0, 100)]
    transcript, output = ev.vendor_inputs(truth, None)
    transcript["words"][0]["end"] = 200  # heard to 200 ms: 10 extra frames over 10 true ones
    assert ev.score(truth, (transcript, output))["merge DER %"] == 100.0


def test_trx01_a_one_line_transcript_breaks_no_line_wrongly() -> None:
    truth = [word("Hello", 0, 100), word("there.", 200, 300)]
    assert ev.score(truth, ev.vendor_inputs(truth, None))["line breaks F1 %"] == 100.0


def test_trx04_filler_precision_is_over_the_words_we_flagged() -> None:
    truth = [word("um", 0, 10, filler=True), word("so", 20, 30)]
    heard = [word("um", 0, 10, filler=True), word("so", 20, 30, filler=True)]
    precision, recall = ev.filler_scores(truth, heard, [(0, 0), (1, 1)])
    assert (precision, recall) == (50.0, 100.0)


def test_trx04_pause_precision_is_over_the_pauses_we_offered() -> None:
    truth = [word("a", 0, 100), word("b", 1200, 1300), word("c", 1400, 1500)]
    heard = [word("a", 0, 100), word("b", 1200, 1300), word("c", 2600, 2700)]
    # We offer two pauses (100 to 1200 and 1300 to 2600): one of them is a true one, and the true one is found.
    found, _ = ev.pause_scores(truth, heard)
    assert round(found, 2) == 66.67


def test_trx01_cp_wer_over_the_true_words_not_the_heard_ones() -> None:
    truth = [word("a", 0, 10, "A"), word("b", 20, 30, "A")]
    heard = [word(w, n * 20, n * 20 + 10, "X") for n, w in enumerate("a b c d".split())]
    assert ev.cp_wer(truth, heard) == 100.0


def test_trx01_cp_wer_counts_a_speaker_nobody_said_as_all_errors_over_the_true_words() -> None:
    truth = [word("a", 0, 10, "A"), word("b", 20, 30, "A")]
    heard = [word("a", 0, 10, "X"), word("b", 20, 30, "X")] + [word("z", n * 20, n * 20 + 10, "Y") for n in range(4)]
    assert ev.cp_wer(truth, heard) == 200.0


def test_trx01_live_der_reads_pyannotes_seconds_as_milliseconds() -> None:
    truth = [word("one", 0, 1000, "A")]
    transcript, _ = ev.vendor_inputs(truth, None)
    output = {"exclusiveDiarization": [{"speaker": "SPEAKER_00", "start": 0.0, "end": 1.0}]}
    row = ev.live_score(ev.Excerpt("T0-0", 0, 600_000, truth, Path("/nowhere")), 30.0, (transcript, output))
    assert row["DER %"] == 0.0


def test_trx01_an_excerpt_costing_exactly_what_is_left_is_sent(tmp_path: Path) -> None:
    heard = Heard()
    left = 600_000 / ev.HOUR_MS * ev.PRICE_PER_HOUR
    rows = asyncio.run(ev.live(excerpts(2), heard, tmp_path / "spend.jsonl", left, tmp_path / "recorded"))
    assert heard.asked == ["T0-0"] and list(rows) == ["T0-0 live"]


def test_trx01_the_ledger_is_made_in_a_folder_that_does_not_exist_yet(tmp_path: Path) -> None:
    ledger = tmp_path / "new" / "folder" / "spend.jsonl"
    asyncio.run(ev.live(excerpts(1), Heard(), ledger, 5.0, tmp_path / "recorded"))
    assert json.loads(ledger.read_text())["items"] == 1


def test_trx01_the_table_prints_two_decimals() -> None:
    assert "1.23" in ev.table({"row": {"metric": 1.234}})
    assert "1.2 " not in ev.table({"row": {"metric": 1.234}}) + " "


def test_trx01_the_all_row_keeps_two_decimals_of_the_mean() -> None:
    rows = {"a": {"m": 0.0}, "b": {"m": 0.0}, "c": {"m": 100.0}}
    assert ev.averaged(rows) == {"m": 33.33}


def test_trx01_an_excerpt_is_cut_from_its_start_for_its_length(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import wave

    rate = 8000
    source = tmp_path / "meeting.wav"
    with wave.open(str(source), "wb") as made:
        made.setparams((1, 2, rate, 0, "NONE", "not compressed"))
        # Two seconds: the first second all 1s, the second all 2s.
        made.writeframes(b"\x01\x00" * rate + b"\x02\x00" * rate)
    monkeypatch.setattr(ev, "CACHE", tmp_path)
    clip = ev.cut(ev.Excerpt("T0-1", 1000, 500, [], source))
    with wave.open(str(clip), "rb") as read:
        assert read.getnframes() == rate // 2
        assert set(read.readframes(read.getnframes())[::2]) == {2}


def test_trx01_a_key_that_is_set_but_empty_is_a_missing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ASSEMBLYAI_API_KEY", "")
    monkeypatch.setenv("PYANNOTE_API_KEY", "x")
    with pytest.raises(SystemExit, match="ASSEMBLYAI_API_KEY"):
        ev.main(["--live", "--budget-usd", "1"])


def test_trx01_a_seeded_extra_word_repeats_without_its_mark_and_always_takes_time() -> None:
    truth = [word("Hello.", 0, 1, "A")]
    extras = []
    for seed in range(4000):
        transcript, _ = ev.vendor_inputs(truth, f"s{seed}")
        heard = transcript["words"]
        if len(heard) == 2:
            extras.append(heard)
    assert extras, "an extra word came up in 4,000 seeds"
    for first, second in extras:
        assert first["text"] == "Hello" and second["text"] == "Hello."
        assert first["end"] > first["start"], first


def test_trx01_a_turn_shifted_by_the_vendors_error_is_never_shorter_than_a_frame() -> None:
    truth = [word("quick", 0, 300, "A")]
    spans = [turn for seed in range(2000) for turn in ev.vendor_inputs(truth, f"t{seed}")[1]["exclusiveDiarization"]]
    assert spans, "turns come out"
    assert all(turn["end"] - turn["start"] >= 0.01 - 1e-9 for turn in spans)


def test_trx01_boundary_errors_are_the_median_and_95th_percentile_of_starts_and_ends() -> None:
    truth = [word("a", 0, 100), word("b", 200, 300)]
    heard = [word("a", 10, 130), word("b", 250, 300)]
    assert ev.boundary_errors(truth, heard, [(0, 0), (1, 1)]) == (20.0, 50.0)
    assert ev.boundary_errors([], [], []) == (0.0, 0.0)


def test_trx01_a_caption_is_wrong_for_two_speakers_or_for_a_pause_and_right_otherwise() -> None:
    truth = [word("a", 0, 100, "A"), word("b", 150, 250, "A"), word("c", 300, 400, "B"), word("d", 1300, 1400, "A")]
    pairs = [(i, i) for i in range(4)]
    group = lambda *items: [list(items)]  # noqa: E731
    assert ev.caption_violations(truth, truth, group(truth[0], truth[1]), pairs) == 0.0
    assert ev.caption_violations(truth, truth, group(truth[1], truth[2]), pairs) == 100.0
    assert ev.caption_violations(truth, truth, group(truth[0], truth[3]), pairs) == 100.0
    assert ev.caption_violations(truth, truth, group(truth[0], truth[1]) + group(truth[2], truth[3]), pairs) == 50.0


def test_trx01_a_short_word_heard_twice_gives_the_extra_a_length() -> None:
    truth = [word("a", 0, 1, "A")]
    extras = 0
    for seed in range(4000):
        transcript, _ = ev.vendor_inputs(truth, str(seed))
        heard = transcript["words"]
        if len(heard) == 2:
            extras += 1
            assert heard[0]["end"] > heard[0]["start"] and heard[1]["start"] == heard[0]["end"]
    assert extras > 0


def test_trx01_an_excerpt_is_charged_before_it_is_sent_so_a_crash_still_costs(tmp_path: Path) -> None:
    class Crashing:
        async def __call__(self, excerpt: Any) -> Any:
            raise RuntimeError("the vendor died")

    ledger = tmp_path / "spend.jsonl"
    with pytest.raises(RuntimeError, match="the vendor died"):
        asyncio.run(ev.live(excerpts(1), Crashing(), ledger, 5.0, tmp_path / "recorded"))
    entry = json.loads(ledger.read_text())
    assert entry["items"] == 1 and entry["note"] == "crashed"
    assert entry["usd"] == pytest.approx(600_000 / ev.HOUR_MS * ev.PRICE_PER_HOUR)
