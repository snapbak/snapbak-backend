"""OpenAI and OpenAI-compatible chat completions, such as Groq (docs/providers.md)."""

import hashlib
from typing import Any

from snapbak_backend.ai.providers.base import Http, Turn, Usage


class OpenAICompatible(Http):
    """`fast` (OpenAI only, founder 13 Oct) asks for priority processing and keys the prompt cache on the fixed prompt,
    so every request of one prompt version lands where its long prefix is cached."""

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
            body["service_tier"] = "priority"
            body["prompt_cache_key"] = "snapbak-" + hashlib.sha256(fixed.encode()).hexdigest()[:16]
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
