"""Polish: the tier, the fallback, and one big-tier correction of a refused answer (docs/api.md)."""

import asyncio
import time

from snapbak_backend.ai import chain, routing
from snapbak_backend.ai.chain import Call, ProviderFailed, Tiers, ask, deadline_of, elapsed_ms, total
from snapbak_backend.ai.guardrail.shape import in_style, plain_bullets
from snapbak_backend.schemas.polish import PolishRequest, PolishResponse
from snapbak_backend.transformers.polish import correction_turn, fold, polish_turns, read

# A correction needs at least a typical big answer's time (gpt-6-luna: p50 1.49 s without reasoning on 30 corpus
# items, 11 Oct 2026), or it mostly times out and only delays the rules text. A refused big answer is corrected too:
# gpt-6-luna sometimes repairs a garbled phrase with an unheard word, and told which word, takes it out (D1).
MIN_RETRY_S = 1.5
# Every chunk is polished with reasoning at its route's effort (`routing.EFFORTS`); its reasoning tokens count against
# the output budget, so they get room of their own: 1,000 cut off some answers at medium effort and left them empty.
REASONING_TOKENS = 4000
# A correction starts late, often 10 s into a long dictation, and the big tier reasoning over it can run past the
# deadline before `HEDGE_S`; the fallback joins it once this share of the time left has gone (round b5: 4 of 8 long
# refusals ended with the correction cut at about 19.75 s and the text typed as said).
RETRY_HEDGE_SHARE = 0.5


async def polish(request: PolishRequest, tiers: Tiers) -> tuple[PolishResponse, list[Call]]:
    """A chunk polished whole, or, when a paragraph is said between long paragraphs, one call per paragraph at once
    (founder, 13 Oct): the chunk then takes its slowest paragraph, and a refused paragraph alone comes back as said."""
    parts = routing.paragraphs(request.text)
    if len(parts) == 1:
        return await polished_chunk(request, tiers)
    started = time.monotonic()
    asked = [
        request.model_copy(update={"text": part, "prior": request.prior if index == 0 else ""})
        for index, part in enumerate(parts)
    ]
    answered = await asyncio.gather(*(polished_chunk(part, tiers) for part in asked))
    responses = [response for response, _ in answered]
    calls = [call for _, made in answered for call in made]
    response = PolishResponse(
        text="\n\n".join(part.text for part in responses),
        revision=responses[0].revision,
        rejected=all(part.rejected for part in responses),
        engine=responses[0].engine,
        tier="big" if any(part.tier == "big" for part in responses) else "small",
        ms=elapsed_ms(started),
        tokens=total(calls),
    )
    return response, calls


async def polished_chunk(request: PolishRequest, tiers: Tiers) -> tuple[PolishResponse, list[Call]]:
    started = time.monotonic()
    deadline = deadline_of(started, request.deadline_ms)
    # The guardrail reads lists as "- " and "1. "; a chat app's round bullets typed before are read as dashes, and the
    # answer is written for the app's style once it is accepted (founder, 12 Oct).
    request = request.model_copy(update={"prior": plain_bullets(request.prior)})
    asked = fold(request)
    turns = polish_turns(asked)
    tier = routing.tier(request.text)
    reason = routing.effort(request.text)
    max_tokens = len(request.text) + len(request.prior) + 100 + REASONING_TOKENS
    calls: list[Call] = []
    first = tiers.chain(tier)
    output = await ask(first, turns, max_tokens, deadline, calls, reason)
    polished = read(request, asked, output)
    calls[-1].verdict = polished.rejected or "ok"
    if polished.rejected and polished.reason and deadline - time.monotonic() >= MIN_RETRY_S:
        # One correction: the refused answer and why, so the model fixes what the guardrail named (D1). Refused again,
        # the text goes in as said.
        corrected = [*turns, ("assistant", output), correction_turn(asked, polished.reason)]
        hedge_s = min(chain.HEDGE_S, (deadline - time.monotonic()) * RETRY_HEDGE_SHARE)
        try:
            effort = routing.EFFORTS["correction"]
            output = await ask(tiers.chain("big"), corrected, max_tokens, deadline, calls, effort, hedge_s)
            tier = "big"
            polished = read(request, asked, output)
            calls[-1].verdict = polished.rejected or "ok"
        except ProviderFailed:
            pass
    answered = next(call for call in reversed(calls) if call.verdict != "error")
    response = PolishResponse(
        text=in_style(polished.text, request.style),
        revision=None if polished.revision is None else in_style(polished.revision, request.style),
        rejected=polished.rejected is not None,
        engine=answered.engine,
        tier=tier,
        ms=elapsed_ms(started),
        tokens=total(calls),
    )
    return response, calls
