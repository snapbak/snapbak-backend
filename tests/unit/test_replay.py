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


def test_a_month_polishes_every_chunk_on_the_one_model_at_the_priority_estimate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(replay, "CACHE_HIT", 0)
    totals = replay.month(["ship it"], "openai:gpt-4.1-nano", "openai:gpt-4.1")
    # The fixed prompt with every module and example is 10,213 tokens since 15 Oct split it into a core and modules
    # (10,201 before; 13,619 before 14 Oct made Snapbak English only, 13,070 since round b6, 12,356 since round b5,
    # 11,444 since round b4, 10,081 since round b3, 6,807 since round b2 and 4,667 since round b1). The founder put
    # accuracy over cost (D8): the month is a report, so the prompt may grow. OpenAI calls are billed at the priority
    # estimate, twice the list price (back since R7, 15 Oct).
    assert (totals["requests"], totals["input"], totals["output"]) == (9000, 92_007_000, 90_000)
    assert totals["polish $"] == pytest.approx(18.4734)


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
    assert totals["polish $"] == pytest.approx(4.68585)


def test_a_chunk_with_a_cue_costs_what_any_chunk_costs_on_the_one_model(monkeypatch: pytest.MonkeyPatch) -> None:
    # Before 15 Oct a cue sent the chunk to the big tier, which reasoned 46 tokens more.
    monkeypatch.setattr(replay, "CACHE_HIT", 0)
    plain = replay.month(["ship it today"], "openai:gpt-4.1-nano", "openai:gpt-4.1")
    corrected = replay.month(["ship it actually today"], "openai:gpt-4.1-nano", "openai:gpt-4.1")
    assert plain["output"] == 6000 * 11
    assert corrected["output"] == 4500 * 14


def test_half_the_fixed_prompt_is_billed_as_cached_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(replay, "RETRY_SHARE", 0)
    totals = replay.month(["ship it"], "openai:gpt-4.1-nano", "openai:gpt-4.1")
    assert totals["polish $"] == pytest.approx(11.5803)


def test_the_month_is_priced_for_the_servers_defaults_and_reported_not_gated(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The founder (11 Oct): accuracy over cost and time, so the month is a report, not a gate.
    monkeypatch.setattr(sys, "argv", ["replay.py"])
    assert replay.main() == 0
    assert "polish openai:gpt-6-luna, correction openai:gpt-6-luna" in capsys.readouterr().out


def test_a_refused_answer_is_corrected_on_the_correction_model_with_its_answer_and_the_reasons(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # D1: the correction asks again, with reasoning, sending the request, the refused answer and why.
    monkeypatch.setattr(replay, "CACHE_HIT", 0)
    english = replay.month(["ship it"], "openai:gpt-4.1-nano", "openai:gpt-4.1")
    request = replay.STATIC + replay.estimate(
        replay.message({"text": "ship it", "prior": "", "terms": [], "app": "", "style": "chat"})
    )
    answer = replay.estimate('{"revision": null, "text": "ship it"}')
    corrected = request + answer + replay.CORRECTION_TOKENS
    one = replay.billed("openai:gpt-4.1", corrected, answer + replay.REASONED_TOKENS, 0)
    assert english["correction $"] == pytest.approx(9000 * replay.RETRY_SHARE * one)


def test_a_minutes_run_prices_the_whole_transcript_in_each_section_s_call_plus_the_repair_share() -> None:
    # MEETINGS.md 11.5: the minutes' cost per meeting, priced offline from the prompts the server sends.
    half_hour = replay.minutes_run(["ship it today"], 1800, "openai:gpt-6-luna")
    two_hours = replay.minutes_run(["ship it today"], 7200, "openai:gpt-6-luna")
    assert half_hour["output"] == replay.SECTION_OUTPUT * 10
    assert two_hours["input"] > 3 * half_hour["input"]
    # Ten calls each resend the transcript (minutes v2): about 4 cents for 30 min, 11 for two hours.
    assert 0 < half_hour["$"] < 0.05 < two_hours["$"] < 0.15
