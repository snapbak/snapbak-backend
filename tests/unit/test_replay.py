import sys

import pytest
import replay


def test_a_hold_is_cut_into_chunks_each_sent_with_the_words_before_it() -> None:
    said = " ".join(f"w{n}" for n in range(40))
    chunks = replay.chunks(said)
    assert [len(text.split()) for _, text in chunks] == [15, 15, 10]
    assert [len(prior.split()) for prior, _ in chunks] == [0, 15, 30]
    assert chunks[1] == (" ".join(f"w{n}" for n in range(15)), " ".join(f"w{n}" for n in range(15, 30)))


def test_prior_is_capped_at_300_words() -> None:
    said = " ".join(["word"] * 400)
    assert max(len(prior.split()) for prior, _ in replay.chunks(said)) == 300


def test_a_short_hold_is_one_chunk() -> None:
    assert replay.chunks("ship it") == [("", "ship it")]


def test_a_month_of_english_retries_a_share_on_the_big_tier(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(replay, "CACHE_HIT", 0)
    totals = replay.month(["ship it"], "openai:gpt-4.1-nano", "openai:gpt-4.1")
    # The fixed prompt is 10,201 tokens since 14 Oct made Snapbak English only and swapped the other languages' rules
    # and examples for English ones (it was 13,619 before, 13,070 since round b6, 12,356 since round b5, 11,444 since
    # round b4, 10,081 since round b3, 6,807 since round b2 and 4,667 since round b1). The founder put accuracy over
    # cost (D8): the month is a report, so the prompt may grow.
    assert (totals["requests"], totals["big"], totals["input"], totals["output"]) == (9000, 0, 91_899_000, 90_000)
    assert totals["small $"] == pytest.approx(18.4518)
    assert totals["big $"] == pytest.approx(19.0116)


def test_utterances_leave_out_the_generated_suite_growth_rows() -> None:
    # The month models a user's speech; suite-growth holds generated eval rows from the inbox, so its share of the
    # corpus would move the month with every intake (11 Oct).
    pool = set(replay.utterances())
    growth = {item.text for item in replay.load() if item.slice == "suite-growth"}
    assert growth
    assert pool.isdisjoint(growth)


def test_the_cached_share_of_the_fixed_prompt_is_billed_at_the_cached_price(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(replay, "CACHE_HIT", 1.0)
    monkeypatch.setattr(replay, "RETRY_SHARE", 0)
    totals = replay.month(["ship it"], "openai:gpt-4.1-nano", "openai:gpt-4.1")
    assert totals["small $"] == pytest.approx(4.68045)


def test_a_correction_chunk_bills_its_reasoning_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(replay, "CACHE_HIT", 0)
    plain = replay.month(["ship it today"], "openai:gpt-4.1-nano", "openai:gpt-4.1")
    corrected = replay.month(["ship it actually today"], "openai:gpt-4.1-nano", "openai:gpt-4.1")
    assert plain["output"] == 6000 * 11
    assert corrected["output"] == 4500 * (14 + 46)


def test_half_the_fixed_prompt_is_billed_as_cached_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(replay, "RETRY_SHARE", 0)
    totals = replay.month(["ship it"], "openai:gpt-4.1-nano", "openai:gpt-4.1")
    assert totals["small $"] == pytest.approx(11.5668)


def test_the_month_is_priced_for_the_founders_pair_and_reported_not_gated(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The founder (11 Oct): same models, accuracy over cost and time, so the month is a report, not a gate.
    monkeypatch.setattr(sys, "argv", ["replay.py"])
    assert replay.main() == 0
    assert "small openai:gpt-5.6-luna, big openai:gpt-6-luna" in capsys.readouterr().out


def test_a_refused_answer_on_either_tier_is_corrected_on_the_big_tier_with_its_answer_and_the_reasons(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # D1: the correction asks the big tier again, with reasoning, sending the request, the refused answer and why.
    monkeypatch.setattr(replay, "CACHE_HIT", 0)
    english = replay.month(["ship it"], "openai:gpt-4.1-nano", "openai:gpt-4.1")
    request = replay.STATIC + replay.estimate(
        replay.message({"text": "ship it", "prior": "", "terms": [], "app": "", "style": "chat"})
    )
    answer = replay.estimate('{"revision": null, "text": "ship it"}')
    corrected = request + answer + replay.CORRECTION_TOKENS
    one = replay.billed("openai:gpt-4.1", corrected, answer + replay.REASONED_TOKENS, 0)
    assert english["big $"] == pytest.approx(9000 * replay.RETRY_SHARE * one)
