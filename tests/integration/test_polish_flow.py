import asyncio
import json
import time

import pytest

from snapbak_backend.ai import chain
from snapbak_backend.ai.chain import ProviderFailed, Tiers
from snapbak_backend.ai.prompts.polish import system
from snapbak_backend.ai.providers.base import ProviderError, Turn, Usage
from snapbak_backend.ai.providers.fake import Fake
from snapbak_backend.schemas.command import CommandRequest
from snapbak_backend.schemas.polish import PolishRequest, PolishResponse
from snapbak_backend.services.command import command
from snapbak_backend.services.polish import polish

USAGE = Usage(100, 10)


class Scripted:
    """A provider that answers with a fixed output after a delay, or raises."""

    def __init__(self, engine: str, output: str | None, delay: float = 0.0, usage: Usage = USAGE) -> None:
        self.engine = engine
        self.output = output
        self.delay = delay
        self.usage = usage
        self.calls = 0
        self.max_tokens = 0
        self.reasoned = ""
        self.asked: dict[str, str] = {}
        self.turns: list[Turn] = []

    async def complete(self, turns: list[Turn], max_tokens: int, timeout: float, reason: str = "") -> tuple[str, Usage]:
        self.calls += 1
        self.max_tokens = max_tokens
        self.reasoned = reason
        self.turns = turns
        # The request is the last user turn; a correction follows it with the refused answer and the reasons.
        self.asked = json.loads(next(content for role, content in reversed(turns) if content.startswith("{")))
        await asyncio.sleep(self.delay)
        if self.output is None:
            raise ProviderError(self.engine)
        return self.output, self.usage

    async def aclose(self) -> None:
        pass


def said(text: str, **changes: object) -> PolishRequest:
    fields = {"text": text, "prior": "", "terms": [], "app": "", "style": "chat"}
    return PolishRequest.model_validate({**fields, "deadline_ms": 2000, **changes})


def answer(text: str, revision: str | None = None) -> str:
    return json.dumps({"revision": revision, "text": text}, ensure_ascii=False)


UNUSED = Scripted("test:unused", None)


def corrected_by(model: Scripted, polish: Scripted | Fake, fallback: Scripted | None = None) -> Tiers:
    """The one polish model, with `model` correcting a refused answer."""
    return Tiers(polish=polish, command=UNUSED, fallback=fallback, correction=model)


@pytest.mark.anyio
async def test_a_chunk_reasons_at_medium_by_default() -> None:
    # Founder, 14 Oct: English thinks at medium, until the bench picks the one cheap model's effort (15 Oct).
    small = Scripted("test:small", answer("Ship it today."))
    response, _ = await polish(said("ship it today"), Tiers(polish=small, command=UNUSED, fallback=None))
    assert (response.text, small.reasoned) == ("Ship it today.", "medium")


@pytest.mark.anyio
async def test_a_plain_chunk_is_polished_by_the_polish_model() -> None:
    tiers = Tiers(polish=Fake("edit"), command=UNUSED, fallback=None)
    response, calls = await polish(said("ship it."), tiers)
    assert (response.text, response.revision, response.rejected) == ("ship it!", None, False)
    assert (response.engine, response.tier, response.tokens) == ("fake:edit", "small", 0)
    assert [call.engine for call in calls] == ["fake:edit"]
    assert UNUSED.calls == 0


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("text", "output"),
    [("deploy on tuesday actually wednesday", "Deploy on Wednesday."), ("ship it no wait send it", "Send it.")],
)
async def test_a_chunk_with_a_correction_cue_is_polished_by_the_one_polish_model(text: str, output: str) -> None:
    # 15 Oct 2026: one model polishes every chunk; a cue no longer sends it to the big model.
    model = Scripted("test:polish", answer(output))
    big = Scripted("test:big", None)
    response, _ = await polish(said(text), Tiers(polish=model, command=big, fallback=None, correction=big))
    assert (response.text, response.tier, response.engine, response.rejected) == (
        output,
        "small",
        "test:polish",
        False,
    )
    assert (model.calls, model.reasoned, big.calls) == (1, "medium", 0)


@pytest.mark.anyio
async def test_a_revision_of_prior_text_comes_back() -> None:
    big = Scripted("test:big", answer("Bring the deck.", revision="Let's meet on Thursday at noon."))
    prior = "Let's meet on Friday at noon. Bring the deck."
    request = said("no wait make it thursday not friday", prior=prior)
    response, _ = await polish(request, Tiers(polish=big, command=UNUSED, fallback=None))
    assert (response.text, response.revision, response.rejected) == (
        "Bring the deck.",
        "Let's meet on Thursday at noon.",
        False,
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    "bad",
    [
        answer("Sure, here it is: ship it."),
        "not json",
        '{"revision": null}',
        "[1]",
        '{"revision": null, "text": 5}',
        '{"revision": 7, "text": "Ship it."}',
    ],
)
async def test_a_refused_answer_is_retried_on_the_correction_model(bad: str) -> None:
    small = Scripted("test:small", bad)
    big = Scripted("test:big", answer("Ship it."), usage=Usage(200, 20))
    response, calls = await polish(said("ship it"), corrected_by(big, small))
    assert (response.text, response.tier, response.engine, response.rejected) == (
        "Ship it.",
        "big",
        "test:big",
        False,
    )
    assert response.tokens == 330
    assert [call.engine for call in calls] == ["test:small", "test:big"]


@pytest.mark.anyio
async def test_a_model_that_corrects_its_own_refused_answer_does_it_once_with_reasoning() -> None:
    # gpt-6-luna sometimes "repairs" a garbled phrase with an unheard word; told which word, it takes it out (D1, 11
    # Oct). The correction reasons: accuracy over speed (D7), with 20 s to answer (D2).
    big = Scripted("test:big", answer("Sure, send it."))
    outputs = iter([answer("Sure, send it."), answer("Send it.")])

    async def complete(turns: list[Turn], max_tokens: int, timeout: float, reason: str = "") -> tuple[str, Usage]:
        big.calls += 1
        big.reasoned = reason
        return next(outputs), USAGE

    big.complete = complete  # type: ignore[method-assign]
    request = said("ship it no wait send it", deadline_ms=6000)
    response, calls = await polish(request, corrected_by(big, big))
    assert (response.text, response.rejected, response.tier) == ("Send it.", False, "big")
    assert [call.verdict for call in calls] == ["reply", "ok"]
    assert (big.calls, big.reasoned) == (2, "medium")


@pytest.mark.anyio
async def test_a_refused_answer_is_corrected_with_the_answer_and_the_words_the_guardrail_named() -> None:
    small = Scripted("test:small", answer("Okay, ship it today."))
    big = Scripted("test:big", answer("Ship it today."))
    response, calls = await polish(said("ship it today"), corrected_by(big, small))
    assert (response.text, response.rejected, response.tier) == ("Ship it today.", False, "big")
    assert [call.verdict for call in calls] == ["unheard", "ok"]
    asked, refused, correction = big.turns[-3:]
    assert asked == small.turns[-1]
    assert refused == ("assistant", answer("Okay, ship it today."))
    assert correction[0] == "user"
    assert "'okay'" in correction[1]
    assert "never said" in correction[1]


@pytest.mark.anyio
async def test_a_correction_asks_the_same_request_again_with_the_checker_note_in_its_own_field() -> None:
    # founder-019 run 2 and formatting-214: a note sent as a turn of its own was polished and sent back as the
    # dictation; inside the request, "text" stays the one thing to polish (round b6).
    small = Scripted("test:small", answer("Okay, ship it today."))
    big = Scripted("test:big", answer("Ship it today."))
    await polish(said("ship it today"), corrected_by(big, small))
    role, content = big.turns[-1]
    fields = json.loads(content)
    note = fields.pop("checker")
    assert role == "user"
    assert fields == json.loads(small.turns[-1][1])
    assert "'okay'" in note
    assert "never said" in note


@pytest.mark.anyio
async def test_a_correction_refused_again_skips_the_ai_after_one_try() -> None:
    small = Scripted("test:small", answer("We ship it."))
    big = Scripted("test:big", answer("We ship it now."))
    response, calls = await polish(said("we shipped it"), corrected_by(big, small))
    assert (response.text, response.rejected) == ("we shipped it", True)
    assert [call.verdict for call in calls] == ["tense", "unheard"]
    assert big.calls == 1
    assert "'shipped'" in big.turns[-1][1]


@pytest.mark.anyio
async def test_a_malformed_answer_is_corrected_with_the_shape_it_needs() -> None:
    small = Scripted("test:small", "Ship it.")
    big = Scripted("test:big", answer("Ship it."))
    response, _ = await polish(said("ship it"), corrected_by(big, small))
    assert (response.text, response.rejected) == ("Ship it.", False)
    assert big.turns[-2] == ("assistant", "Ship it.")
    assert "JSON" in big.turns[-1][1]


@pytest.mark.anyio
async def test_a_stutter_dropped_from_a_counted_list_is_accepted() -> None:
    # The founder's dictation of 11 Oct, as the app sent its last chunk, and gpt-6-luna's answer to it.
    prior = (
        "Ok, so this is just a simple test that I am running. First, it needs to be an audit set. "
        "Second I want to buy a chair."
    )
    revision = (
        "Ok, so this is just a simple test that I am running:\n1. It needs to be an audit set\n"
        "2. I want to buy a chair\n3. Let's get eggs for the breakfast."
    )
    model = Scripted("test:small", answer("It's finished.", revision))
    request = said("Third lets get eggs for the breakfast. It's it's finished finished.", prior=prior)
    response, _ = await polish(request, Tiers(polish=model, command=UNUSED, fallback=None))
    assert (response.revision, response.text, response.rejected) == (revision, "It's finished.", False)


@pytest.mark.anyio
async def test_when_the_answer_and_its_correction_are_refused_the_request_text_comes_back() -> None:
    small = Scripted("test:small", answer("Sure, ship it."))
    big = Scripted("test:big", answer("Of course, ship it."))
    response, _ = await polish(said("ship it"), corrected_by(big, small))
    assert (response.text, response.revision, response.rejected, response.tier) == (
        "ship it",
        None,
        True,
        "big",
    )


@pytest.mark.anyio
async def test_a_failed_correction_keeps_the_first_refusal() -> None:
    small = Scripted("test:small", answer("Sure, ship it."))
    big = Scripted("test:big", None)
    response, _ = await polish(said("ship it"), corrected_by(big, small))
    assert (response.text, response.rejected, response.tier, response.engine) == (
        "ship it",
        True,
        "small",
        "test:small",
    )


@pytest.mark.anyio
async def test_a_provider_error_tries_the_fallback_once() -> None:
    fallback = Scripted("test:fallback", answer("Ship it."))
    tiers = Tiers(polish=Fake("fail"), command=UNUSED, fallback=fallback)
    response, calls = await polish(said("ship it"), tiers)
    assert (response.text, response.engine, response.tier) == ("Ship it.", "test:fallback", "small")
    assert [(call.engine, call.verdict) for call in calls] == [
        ("fake:fail", "error"),
        ("test:fallback", "ok"),
    ]


@pytest.mark.anyio
async def test_every_provider_failing_raises() -> None:
    tiers = Tiers(polish=Fake("fail"), command=UNUSED, fallback=Scripted("test:fallback", None))
    with pytest.raises(ProviderFailed) as failed:
        await polish(said("ship it"), tiers)
    assert (failed.value.engine, failed.value.verdict) == ("fake:fail", "error>error")


@pytest.mark.anyio
async def test_a_tier_that_times_out_is_named_and_says_timeout() -> None:
    tiers = Tiers(polish=Fake("slow"), command=UNUSED, fallback=Fake("echo"))
    with pytest.raises(ProviderFailed) as failed:
        await polish(said("ship it", deadline_ms=400), tiers)
    assert (failed.value.engine, failed.value.verdict) == ("fake:slow", "timeout")


@pytest.mark.anyio
async def test_a_slow_tier_is_backed_up_by_the_fallback_and_the_first_answer_wins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 11 Oct: gpt-6-luna once took over 5.75 s on a dictation it usually answers in 2.5 s; the fallback, asked
    # once the tier is late, answers in about 1.5 s without reasoning.
    monkeypatch.setattr(chain, "HEDGE_S", 0.1)
    tier = Scripted("test:small", answer("Ship it now."), delay=2.0)
    fallback = Scripted("test:fallback", answer("Ship it."), delay=0.05)
    started = time.monotonic()
    request = said("ship it", deadline_ms=6000)
    response, calls = await polish(request, Tiers(polish=tier, command=UNUSED, fallback=fallback))
    assert time.monotonic() - started < 0.6
    assert (response.text, response.engine) == ("Ship it.", "test:fallback")
    assert (fallback.calls, fallback.reasoned) == (1, "")
    assert [call.engine for call in calls if call.verdict == "ok"] == ["test:fallback"]


@pytest.mark.anyio
async def test_a_tier_that_answers_before_the_backup_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(chain, "HEDGE_S", 0.1)
    tier = Scripted("test:small", answer("Ship it."), delay=0.3)
    fallback = Scripted("test:fallback", answer("Ship it now."), delay=2.0)
    started = time.monotonic()
    request = said("ship it", deadline_ms=6000)
    response, _ = await polish(request, Tiers(polish=tier, command=UNUSED, fallback=fallback))
    assert time.monotonic() - started < 0.8
    assert (response.text, response.engine, fallback.calls) == ("Ship it.", "test:small", 1)


@pytest.mark.anyio
async def test_a_tier_that_answers_in_time_never_asks_the_backup() -> None:
    tier = Scripted("test:small", answer("Ship it."), delay=0.05)
    fallback = Scripted("test:fallback", answer("Ship it now."))
    request = said("ship it", deadline_ms=6000)
    response, _ = await polish(request, Tiers(polish=tier, command=UNUSED, fallback=fallback))
    assert (response.engine, fallback.calls) == ("test:small", 0)


@pytest.mark.anyio
async def test_no_fallback_means_a_failure_raises() -> None:
    with pytest.raises(ProviderFailed):
        await polish(said("ship it"), Tiers(polish=Fake("fail"), command=UNUSED, fallback=None))


@pytest.mark.anyio
async def test_a_slow_provider_is_cut_at_the_deadline() -> None:
    tiers = Tiers(polish=Fake("slow"), command=UNUSED, fallback=Fake("echo"))
    started = time.monotonic()
    with pytest.raises(ProviderFailed):
        await polish(said("ship it", deadline_ms=400), tiers)
    assert time.monotonic() - started < 2.0


@pytest.mark.anyio
async def test_a_big_retry_is_cut_before_the_client_gives_up() -> None:
    small = Scripted("test:small", answer("Sure, ship it."), delay=0.1)
    big = Scripted("test:big", answer("Ship it."), delay=5.0)
    started = time.monotonic()
    response, _ = await polish(said("ship it", deadline_ms=2000), corrected_by(big, small))
    assert time.monotonic() - started < 2.0
    assert (response.text, response.rejected, response.tier) == ("ship it", True, "small")
    assert big.calls == 1


@pytest.mark.anyio
async def test_a_slow_big_retry_is_backed_up_by_the_fallback_in_time() -> None:
    # Round b5: a long dictation's correction started about 10 s in and the big tier, still reasoning, was cut at the
    # deadline before the fallback was ever asked, so the text went in as said. The retry is hedged within the time
    # it has left.
    small = Scripted("test:small", answer("Sure, ship it."), delay=0.1)
    big = Scripted("test:big", answer("Ship it now."), delay=5.0)
    fallback = Scripted("test:fallback", answer("Ship it."), delay=0.05)
    started = time.monotonic()
    response, _ = await polish(said("ship it", deadline_ms=4000), corrected_by(big, small, fallback))
    assert time.monotonic() - started < 3.5
    assert (response.text, response.rejected, response.engine) == ("Ship it.", False, "test:fallback")


@pytest.mark.anyio
async def test_no_big_retry_starts_with_too_little_time_left() -> None:
    small = Scripted("test:small", answer("Sure, ship it."), delay=0.2)
    big = Scripted("test:big", answer("Ship it."), delay=0.0)
    response, _ = await polish(said("ship it", deadline_ms=600), corrected_by(big, small))
    assert (response.text, response.rejected, big.calls) == ("ship it", True, 0)


@pytest.mark.anyio
@pytest.mark.parametrize("deadline_ms", [1200, 1800])
async def test_no_big_retry_starts_with_less_time_left_than_a_typical_big_answer(deadline_ms: int) -> None:
    # gpt-6-luna answers a retry, which doesn't reason, in about 1.5 s (p50, 11 Oct 2026).
    small = Scripted("test:small", answer("Sure, ship it."), delay=0.1)
    big = Scripted("test:big", answer("Ship it."), delay=5.0)
    started = time.monotonic()
    response, _ = await polish(said("ship it", deadline_ms=deadline_ms), corrected_by(big, small))
    assert time.monotonic() - started < 0.5
    assert (response.text, response.rejected, big.calls) == ("ship it", True, 0)


@pytest.mark.anyio
async def test_a_small_answer_after_the_deadline_is_a_failure() -> None:
    small = Scripted("test:small", answer("Ship it."), delay=0.4)
    with pytest.raises(ProviderFailed):
        await polish(said("ship it", deadline_ms=300), Tiers(polish=small, command=UNUSED, fallback=None))


@pytest.mark.anyio
async def test_a_command_always_runs_on_the_big_model() -> None:
    small = Scripted("test:small", None)
    asked = CommandRequest(selection="send the deck", instruction="shout", app="", deadline_ms=6000)
    response, _ = await command(asked, Tiers(polish=small, command=Fake("edit"), fallback=None, correction=small))
    assert (response.text, response.engine, response.tokens) == ("SEND THE DECK", "fake:edit", 0)
    assert small.calls == 0


@pytest.mark.anyio
async def test_a_command_falls_back_and_a_malformed_command_answer_fails() -> None:
    asked = CommandRequest(selection="hey", instruction="formal", app="", deadline_ms=6000)
    fallback = Scripted("test:fallback", json.dumps({"text": "Hello."}))
    response, _ = await command(asked, Tiers(polish=UNUSED, command=Fake("fail"), fallback=fallback))
    assert (response.text, response.engine) == ("Hello.", "test:fallback")
    with pytest.raises(ProviderFailed) as failed:
        await command(asked, Tiers(polish=UNUSED, command=Scripted("test:big", "Hello."), fallback=None))
    assert (failed.value.engine, failed.value.verdict) == ("test:big", "malformed")


@pytest.mark.anyio
async def test_the_output_budget_grows_with_the_text_and_prior() -> None:
    small = Scripted("test:small", answer("a" * 500))
    await polish(said("a" * 500, prior="b" * 300), Tiers(polish=small, command=UNUSED, fallback=None))
    big = Scripted("test:big", json.dumps({"text": "x"}))
    asked = CommandRequest(selection="x" * 1000, instruction="shout", app="", deadline_ms=6000)
    await command(asked, Tiers(polish=UNUSED, command=big, fallback=None))
    # English reasons at medium, so it gets the reasoning room too (founder, 14 Oct).
    assert (small.max_tokens, big.max_tokens) == (4900, 2200)


@pytest.mark.anyio
async def test_a_chunk_reasons_with_room_for_the_reasoning_tokens() -> None:
    cue = Scripted("test:polish", answer("Deploy on Wednesday."))
    await polish(said("deploy on tuesday actually wednesday"), Tiers(polish=cue, command=UNUSED, fallback=None))
    plain = Scripted("test:polish", answer("Deploy on Tuesday."))
    await polish(said("deploy on tuesday"), Tiers(polish=plain, command=UNUSED, fallback=None))
    # Round b3: medium reasoning needs room of its own, 4,000 tokens (1,000 cut some answers off empty).
    assert (cue.reasoned, cue.max_tokens) == ("medium", 4136)
    assert (plain.reasoned, plain.max_tokens) == ("medium", 4117)


@pytest.mark.anyio
async def test_a_long_chunk_reasons_with_room_for_the_reasoning_tokens() -> None:
    text = " ".join(["word"] * 151)
    big = Scripted("test:big", answer(text))
    await polish(said(text), Tiers(polish=big, command=UNUSED, fallback=None))
    assert (big.reasoned, big.max_tokens) == ("medium", len(text) + 4100)


@pytest.mark.anyio
async def test_a_command_answer_is_trimmed() -> None:
    big = Scripted("test:big", json.dumps({"text": "  Hello.\n"}))
    asked = CommandRequest(selection="hey", instruction="formal", app="", deadline_ms=6000)
    response, _ = await command(asked, Tiers(polish=UNUSED, command=big, fallback=None))
    assert response.text == "Hello."


async def corrected(prior: str, text: str, output: str) -> tuple[PolishResponse, dict[str, str]]:
    big = Scripted("test:big", output)
    response, _ = await polish(said(text, prior=prior), Tiers(polish=big, command=UNUSED, fallback=None))
    return response, big.asked


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("prior", "text", "fixed", "asked_prior", "asked_text"),
    [
        (
            "We need a list. Let's meet on Friday at noon.",
            "no wait make it thursday",
            "Let's meet on Thursday at noon.",
            "We need a list.",
            "Let's meet on Friday at noon. no wait make it thursday",
        ),
        # The revision is the prior up to the folded sentence with the break trimmed; the app keeps the break
        # from its own prior (DIC-03).
        (
            "Hi team.\n\nLet's meet on Friday.",
            "no wait thursday",
            "Let's meet on Thursday.",
            "Hi team.",
            "Let's meet on Friday. no wait thursday",
        ),
    ],
)
async def test_a_correction_gets_the_last_sentence_of_the_prior_to_fix_in_one_piece(
    prior: str, text: str, fixed: str, asked_prior: str, asked_text: str
) -> None:
    response, asked = await corrected(prior, text, answer(fixed))
    assert (asked["prior"], asked["text"]) == (asked_prior, asked_text)
    assert (response.text, response.revision, response.rejected) == (fixed, asked_prior, False)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("prior", "text", "output", "chunk"),
    [
        (
            "We shipped the fix.",
            "no problem I mean it",
            "We shipped the fix. No problem, I mean it.",
            "No problem, I mean it.",
        ),
    ],
)
async def test_a_correction_that_leaves_the_prior_as_it_was_comes_back_as_the_chunk_alone(
    prior: str, text: str, output: str, chunk: str
) -> None:
    response, _ = await corrected(prior, text, answer(output))
    assert (response.text, response.revision, response.rejected) == (chunk, None, False)


@pytest.mark.anyio
async def test_a_correction_in_a_list_item_folds_only_that_item() -> None:
    response, asked = await corrected("We need:\n1. The API", "scratch that the docs", answer("1. The docs"))
    assert (asked["prior"], asked["text"]) == ("We need:", "1. The API scratch that the docs")
    assert (response.text, response.revision) == ("1. The docs", "We need:")


@pytest.mark.anyio
async def test_a_rejected_correction_keeps_the_request_chunk() -> None:
    output = answer("Sure, let's meet on Thursday at noon.")
    response, _ = await corrected("Let's meet on Friday at noon.", "no wait make it thursday", output)
    assert (response.text, response.revision, response.rejected) == ("no wait make it thursday", None, True)


@pytest.mark.anyio
async def test_a_chunk_without_a_correction_is_sent_with_the_prior_as_it_is() -> None:
    small = Scripted("test:small", answer("after the tests pass."))
    request = said("after the tests pass", prior="We need a list. We should ship the fix")
    response, _ = await polish(request, Tiers(polish=small, command=UNUSED, fallback=None))
    assert (small.asked["prior"], small.asked["text"]) == (
        "We need a list. We should ship the fix",
        "after the tests pass",
    )
    assert (response.text, response.revision) == ("after the tests pass.", None)


ANNOUNCED = "Things for today:\n- Call Rahul\n- Send the deck"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("style", "typed"),
    [
        ("chat", "Things for today:\n• Call Rahul\n• Send the deck"),
        ("email", ANNOUNCED),
        ("code", "Things for today: Call Rahul. Send the deck."),
    ],
)
async def test_a_list_comes_back_written_for_the_apps_style(style: str, typed: str) -> None:
    # Founder, 12 Oct: lists as each app renders them; the guardrail judged the plain form first.
    tiers = Tiers(polish=Scripted("test:small", answer(ANNOUNCED)), command=UNUSED, fallback=None)
    response, _ = await polish(said("things for today call rahul send the deck", style=style), tiers)
    assert (response.text, response.rejected) == (typed, False)


@pytest.mark.anyio
async def test_a_chat_piece_goes_on_with_a_list_typed_with_round_bullets() -> None:
    prior = "Things for today:\n• Call Rahul\n• Send the deck"
    small = Scripted("test:small", answer("- Book the room"))
    tiers = Tiers(polish=small, command=UNUSED, fallback=None)
    response, _ = await polish(said("book the room", prior=prior, style="chat"), tiers)
    assert small.asked["prior"] == ANNOUNCED
    assert (response.text, response.rejected) == ("• Book the room", False)


@pytest.mark.anyio
async def test_a_long_chunk_stays_on_the_polish_model_at_its_effort() -> None:
    text = " ".join(["ship it"] * 80)
    model = Scripted("test:polish", answer(text.capitalize() + "."))
    tiers = Tiers(polish=model, command=UNUSED, fallback=None, effort="low")
    response, _ = await polish(said(text), tiers)
    assert (response.tier, response.engine, model.reasoned) == ("small", "test:polish", "low")


@pytest.mark.anyio
async def test_a_reasoning_call_keeps_its_time_before_the_backup(monkeypatch: pytest.MonkeyPatch) -> None:
    # Every route reasons (14 Oct), and the fallback does not: it joins only after HEDGE_S (round b3).
    monkeypatch.setattr(chain, "HEDGE_S", 5.0)
    reasoning = Scripted("test:small", answer("Ship it."), delay=0.4)
    late = Scripted("test:fallback", answer("Ship it now."), delay=0.05)
    request = said("ship it", deadline_ms=6000)
    response, _ = await polish(request, Tiers(polish=reasoning, command=UNUSED, fallback=late))
    assert (response.engine, late.calls) == ("test:small", 0)


@pytest.mark.anyio
async def test_a_dictation_with_a_said_paragraph_is_polished_one_paragraph_per_call_at_once() -> None:
    first = " ".join(["we ship the build today"] * 6)
    second = " ".join(["they test the app tonight"] * 6)

    class Echo(Scripted):
        async def complete(self, turns, max_tokens, timeout, reason=False):  # type: ignore[override]
            # Each call echoes its own request; the parent's `asked` is shared by calls in flight together.
            text = json.loads(turns[-1][1])["text"]
            await super().complete(turns, max_tokens, timeout, reason)
            return answer(text.capitalize() + "."), self.usage

    small = Echo("test:small", answer("unused"), delay=0.3)
    started = time.monotonic()
    response, _ = await polish(
        said(f"{first} new paragraph {second}"), Tiers(polish=small, command=UNUSED, fallback=None)
    )
    assert time.monotonic() - started < 0.55, "the paragraphs are asked at the same time"
    assert small.calls == 2
    assert response.text == f"{first.capitalize()}.\n\n{second.capitalize()}."
    assert response.rejected is False


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("text", "kept", "mended"),
    [
        ("ship it on friday i mean monday", "Ship it on Friday, I mean, Monday.", "Ship it on Monday."),
        ("print five no wait six copies", "Print five, no wait, six copies.", "Print six copies."),
    ],
)
async def test_both_versions_kept_around_a_cue_are_mended_without_a_second_call(
    text: str, kept: str, mended: str
) -> None:
    # Founder, 13 Oct: the guardrail names the old version, the cue and the new one; the old one and the cue go and
    # the answer is judged again, so no correction call is needed. A mend the guardrail refuses still gets the call.
    big = Scripted("test:big", answer(kept))
    response, _ = await polish(said(text), Tiers(polish=big, command=UNUSED, fallback=None))
    assert (response.text, response.rejected, big.calls) == (mended, False, 1)


@pytest.mark.anyio
async def test_an_answer_that_writes_the_prior_again_is_read_as_the_piece_after_it() -> None:
    # 14 Oct: the model sometimes writes the prior again before the piece, with no revision; the piece's answer is
    # what follows it, so no correction call is needed.
    small = Scripted("test:small", answer("Paris buffer: 0.8%"))
    response, _ = await polish(
        said("zero point eight percent", prior="Paris buffer:"), Tiers(polish=small, command=UNUSED, fallback=None)
    )
    assert (response.rejected, response.revision, small.calls) == (False, None, 1)
    assert response.text.startswith("0.8%")


def test_a_piece_that_opens_with_its_cue_is_not_mended_by_taking_words_out() -> None:
    # The guardrail names only the cue here ("let me rephrase"): the piece needs a revision of the prior, not a cut.
    from snapbak_backend.ai.guardrail.judge import Reason, Reject
    from snapbak_backend.transformers.polish import mendings

    request = said("let me rephrase we meet on friday", prior="We meet on Thursday.")
    reason = Reason(Reject.KEPT_CORRECTION, ("let", "me", "rephrase"))
    assert mendings(request, "Let me rephrase, we meet on Friday.", reason) == []


@pytest.mark.anyio
async def test_with_the_correction_off_a_refused_answer_goes_in_as_said_after_one_call() -> None:
    # SNAPBAK_CORRECTION=none (15 Oct): the bench weighs a retry's accuracy against its time and cost.
    model = Scripted("test:polish", answer("Sure, ship it."))
    response, calls = await polish(
        said("ship it", deadline_ms=6000), Tiers(polish=model, command=UNUSED, fallback=None)
    )
    assert (response.text, response.rejected, response.tier) == ("ship it", True, "small")
    assert [call.verdict for call in calls] == ["reply"]
    assert model.calls == 1


@pytest.mark.anyio
async def test_the_polish_model_reasons_at_the_set_effort_and_a_correction_at_medium() -> None:
    model = Scripted("test:polish", answer("Okay, ship it today."))
    big = Scripted("test:big", answer("Ship it today."))
    tiers = Tiers(polish=model, command=UNUSED, fallback=None, correction=big, effort="none")
    response, _ = await polish(said("ship it today", deadline_ms=6000), tiers)
    assert (response.text, response.tier, response.engine) == ("Ship it today.", "big", "test:big")
    assert (model.reasoned, big.reasoned) == ("none", "medium")


@pytest.mark.anyio
async def test_the_features_pick_the_rules_and_the_policy_the_examples_the_model_reads() -> None:
    model = Scripted("test:polish", answer("Ship it today."))
    tiers = Tiers(polish=model, command=UNUSED, fallback=None, examples="none")
    await polish(said("ship it today", features=["lists", "numbers"]), tiers)
    assert [role for role, _ in model.turns] == ["system", "user"]
    assert model.turns[0] == ("system", system(["grammar", "lists", "numbers"]))
    every = Scripted("test:polish", answer("Ship it today."))
    await polish(said("ship it today"), Tiers(polish=every, command=UNUSED, fallback=None))
    assert every.turns[0] == ("system", system(["grammar", "lists", "numbers", "backtracking", "dictionary"]))
    assert len(every.turns) == 2 + 2 * 92


@pytest.mark.anyio
async def test_each_paragraph_and_a_folded_correction_keep_the_features() -> None:
    first = " ".join(["we ship the build today"] * 6)
    second = " ".join(["they test the app tonight"] * 6)
    model = Scripted("test:polish", answer("unused"))
    request = said(f"{first} new paragraph {second}", features=["grammar"])
    await polish(request, Tiers(polish=model, command=UNUSED, fallback=None))
    assert model.turns[0] == ("system", system(["grammar"]))
    folded = Scripted("test:polish", answer("Meet on Thursday."))
    request = said("no wait thursday", prior="Hi. Meet on Friday.", features=["backtracking"])
    await polish(request, Tiers(polish=folded, command=UNUSED, fallback=None))
    assert folded.turns[0] == ("system", system(["grammar", "backtracking"]))
