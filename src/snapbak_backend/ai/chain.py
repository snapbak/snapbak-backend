"""Asking a tier's providers in turn, then the fallback, before the deadline (docs/api.md)."""

import asyncio
import time
from dataclasses import dataclass

from snapbak_backend.ai import routing
from snapbak_backend.ai.providers.base import Provider, ProviderError, Turn, Usage

# The client stops waiting deadline_ms after it sends; the answer needs this long to travel back.
MARGIN_MS = 250
# A tier not answered by then is joined by the fallback: gpt-6-luna at medium reasoning answers a dictation in about
# 3.4 s at the median and 10.6 s at p95 (round b3, 376 bench rows), and gpt-5.6-luna answers in about 1.5 s without
# it; an earlier hedge let the unreasoned fallback win most long answers.
HEDGE_S = 10.0
# An answer that does not reason is backed up sooner by the fallback, which answers it alike (founder, 13 Oct).
FAST_HEDGE_S = 3.0


@dataclass(frozen=True)
class Tiers:
    small: Provider
    big: Provider
    fallback: Provider | None

    def chain(self, tier: routing.Tier) -> list[Provider]:
        first = self.small if tier == "small" else self.big
        return [first] if self.fallback is None else [first, self.fallback]


@dataclass
class Call:
    """One model call. `output` stays in memory for the bench; it never reaches a log."""

    engine: str
    usage: Usage
    verdict: str
    output: str = ""


class ProviderFailed(Exception):
    """No answer along a chain: `engine` is the first provider asked, `verdict` each call's "error" or "timeout",
    `retries` the 429 and 5xx answers asked again on the way."""

    def __init__(self, engine: str, verdict: str, retries: int = 0) -> None:
        super().__init__(engine)
        self.engine = engine
        self.verdict = verdict
        self.retries = retries


async def complete(
    provider: Provider, turns: list[Turn], max_tokens: int, deadline: float, reason: str
) -> tuple[str, Usage]:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError
    async with asyncio.timeout(remaining):
        answer = await provider.complete(turns, max_tokens, remaining, reason)
    return answer


async def ask(
    chain: list[Provider],
    turns: list[Turn],
    max_tokens: int,
    deadline: float,
    calls: list[Call],
    reason: str = "",
    hedge_s: float | None = None,
) -> str:
    """The first answer along `chain` (a tier, then the fallback) that arrives before the deadline. A tier that fails
    hands over to the fallback; one still asking after `hedge_s` (`HEDGE_S` unless given) is joined by the fallback
    without reasoning, and the first answer wins."""
    first = chain[0]
    backups = chain[1:]
    asking: dict[asyncio.Task[tuple[str, Usage]], Provider] = {
        asyncio.create_task(complete(first, turns, max_tokens, deadline, reason)): first
    }
    hedge_at = time.monotonic() + (HEDGE_S if hedge_s is None else hedge_s)
    failed: list[str] = []
    try:
        while asking:
            until = hedge_at if backups else deadline
            wait = max(min(until, deadline) - time.monotonic(), 0)
            done, _ = await asyncio.wait(asking, timeout=wait, return_when=asyncio.FIRST_COMPLETED)
            if not done and backups and time.monotonic() < deadline:
                backup = backups.pop(0)
                asking[asyncio.create_task(complete(backup, turns, max_tokens, deadline, ""))] = backup
                continue
            if not done:
                failed.extend("timeout" for _ in asking)
                calls.extend(Call(provider.engine, Usage(0, 0), "error") for provider in asking.values())
                break
            for task in done:
                provider = asking.pop(task)
                error = task.exception()
                if error is None:
                    output, usage = task.result()
                    calls.append(Call(provider.engine, usage, "ok", output))
                    return output
                if not isinstance(error, (ProviderError, TimeoutError)):
                    raise error
                failed.append("timeout" if isinstance(error, TimeoutError) else "error")
                retried = error.retries if isinstance(error, ProviderError) else 0
                calls.append(Call(provider.engine, Usage(0, 0, 0, retried), "error"))
                if backups and not asking and time.monotonic() < deadline:
                    backup = backups.pop(0)
                    asking[asyncio.create_task(complete(backup, turns, max_tokens, deadline, reason))] = backup
    finally:
        for task in asking:
            task.cancel()
    verdict = ">".join(failed) or "timeout"
    raise ProviderFailed(first.engine, verdict, retries(calls))


def retries(calls: list[Call]) -> int:
    return sum(call.usage.retries for call in calls)


def total(calls: list[Call]) -> int:
    return sum(call.usage.input + call.usage.output for call in calls)


def elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def deadline_of(started: float, deadline_ms: int) -> float:
    return started + (deadline_ms - MARGIN_MS) / 1000
