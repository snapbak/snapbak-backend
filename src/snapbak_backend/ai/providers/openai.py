"""OpenAI and OpenAI-compatible chat completions, such as Groq (docs/providers.md)."""

import hashlib
from typing import Any

from snapbak_backend.ai.providers.base import Http, Turn, Usage

# The characters of the system prompt the cache key follows: within the polish prompt's core (2,006).
OPENING = 1024


class OpenAICompatible(Http):
    """`fast` (OpenAI only) asks for priority processing and keys the prompt cache on the system prompt's opening, so
    every polish request lands where its shared prefix is cached. R7 (15 Oct 2026) measured the default tier 25 % slower
    at p50 for the same model; priority with the shared key served 70 % of the prompt from the cache."""

    def __init__(
        self, engine: str, base: str, model: str, key: str, sampling: dict[str, Any], fast: bool = False
    ) -> None:
        super().__init__(engine, f"{base}/chat/completions", {"Authorization": f"Bearer {key}"})
        self.model = model
        self.sampling = sampling
        self.fast = fast

    async def complete(self, turns: list[Turn], max_tokens: int, timeout: float, reason: str = "") -> tuple[str, Usage]:
        sampling = self.sampling
        # A call that reasons asks a reasoning model for its route's effort (docs/escalation.md); others ignore it.
        if reason and "reasoning_effort" in sampling:
            sampling = {"reasoning_effort": reason}
        body = {
            "model": self.model,
            **sampling,
            "max_completion_tokens": max_tokens,
            "response_format": {"type": "json_object"},
            "messages": [{"role": role, "content": content} for role, content in turns],
        }
        if self.fast:
            fixed = next((content for role, content in turns if role == "system"), "")
            # Keyed on the opening every polish prompt shares (its core), not the whole prompt: one key per module
            # set split the traffic so thin that 41 % of the prompt was cached (15 Oct, docs/providers.md).
            opening = fixed[:OPENING]
            body["service_tier"] = "priority"
            body["prompt_cache_key"] = "snapbak-" + hashlib.sha256(opening.encode()).hexdigest()[:16]
        answer = await self.ask(body, timeout, self.parse)
        return answer

    @staticmethod
    def parse(answer: Any) -> tuple[str, Usage]:
        text = answer["choices"][0]["message"]["content"]
        counts = answer["usage"]
        cached = counts.get("prompt_tokens_details", {}).get("cached_tokens", 0)
        reasoning = counts.get("completion_tokens_details", {}).get("reasoning_tokens", 0)
        usage = Usage(counts["prompt_tokens"], counts["completion_tokens"], cached, reasoning=reasoning)
        return text, usage
