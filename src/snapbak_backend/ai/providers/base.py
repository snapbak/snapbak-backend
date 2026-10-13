"""The provider interface, `complete(turns, max_tokens, timeout)`, and the pooled HTTP client (docs/providers.md)."""

import asyncio
import contextlib
import random
import time
from collections.abc import Callable
from typing import Any, NamedTuple, Protocol

import httpx

type Turn = tuple[str, str]


# Idle provider connections live 5 minutes, so the next dictation skips the TLS handshake; warming opens one at
# most every 30 s.
LIMITS = httpx.Limits(keepalive_expiry=300)
WARM_EVERY_S = 30
WARM_TIMEOUT_S = 5


# A rate limit (429) or a provider fault (5xx) is asked again before the fallback, within the request's deadline
# (D6): after the provider's Retry-After, else 0.25 s doubling each time, with jitter so retries don't line up. An
# account out of credits answers 429 too, and asking again never helps.
RETRIED = {429, 500, 502, 503, 504}
OUT_OF_CREDITS = ("insufficient_quota", "credit_balance_exhausted")
FIRST_BACKOFF_S = 0.25


class Usage(NamedTuple):
    input: int
    output: int
    cached: int = 0
    retries: int = 0
    # Of `output`, the tokens a reasoning model spent thinking before its answer (OpenAI only).
    reasoning: int = 0


class ProviderError(Exception):
    def __init__(self, message: str, retries: int = 0) -> None:
        super().__init__(message)
        self.retries = retries


class Provider(Protocol):
    engine: str

    async def complete(
        self, turns: list[Turn], max_tokens: int, timeout: float, reason: str = ""
    ) -> tuple[str, Usage]: ...

    async def aclose(self) -> None: ...


class Http:
    """One pooled HTTP/2 client per provider; every failure, status or shape problem is a ProviderError."""

    def __init__(self, engine: str, url: str, headers: dict[str, str]) -> None:
        self.engine = engine
        self.url = url
        self.client = httpx.AsyncClient(http2=True, headers=headers, limits=LIMITS)
        self.warmed = -float("inf")

    async def warm(self) -> None:
        """Opens the connection before a dictation needs it: a HEAD whose answer and failure are ignored."""
        now = time.monotonic()
        if now - self.warmed < WARM_EVERY_S:
            return
        self.warmed = now
        with contextlib.suppress(httpx.HTTPError):
            await self.client.head(self.url, timeout=WARM_TIMEOUT_S)

    async def ask(
        self, body: dict[str, Any], timeout: float, parse: Callable[[Any], tuple[str, Usage]]
    ) -> tuple[str, Usage]:
        """The provider's answer to `body`, parsed, with the times a 429 or 5xx was asked again in its usage."""
        deadline = time.monotonic() + timeout
        retries = 0
        while True:
            try:
                response = await self.client.post(self.url, json=body, timeout=deadline - time.monotonic())
            except httpx.HTTPError as error:
                raise ProviderError(f"{self.engine}: {type(error).__name__}", retries) from error
            wait = backoff(response, retries)
            if wait is None or time.monotonic() + wait >= deadline:
                break
            retries += 1
            await asyncio.sleep(wait)
        try:
            response.raise_for_status()
            text, usage = parse(response.json())
        except (httpx.HTTPError, ValueError) as error:
            raise ProviderError(f"{self.engine}: {type(error).__name__}", retries) from error
        except (KeyError, IndexError, TypeError) as error:
            raise ProviderError(f"{self.engine}: unexpected answer", retries) from error
        return text, usage._replace(retries=retries)

    async def aclose(self) -> None:
        await self.client.aclose()


def backoff(response: httpx.Response, retries: int) -> float | None:
    """How long to wait before asking again, or None when asking again can't help."""
    if response.status_code not in RETRIED or any(word in response.text for word in OUT_OF_CREDITS):
        return None
    jitter = random.uniform(0.5, 1.5)
    after = response.headers.get("retry-after", "")
    try:
        return float(after) * (1 + jitter / 10)
    except ValueError:
        return FIRST_BACKOFF_S * 2**retries * jitter
