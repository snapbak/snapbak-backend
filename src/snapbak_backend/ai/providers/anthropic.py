"""Anthropic messages (docs/providers.md)."""

from typing import Any

from snapbak_backend.ai.providers.base import Http, Turn, Usage

# The rules and the worked examples open every request the same way: cached, a request reads them at a tenth of
# the price. A prefix too short for the model's cache minimum is sent uncached, with no error.
CACHED = {"type": "ephemeral"}


class Anthropic(Http):
    def __init__(self, engine: str, model: str, key: str) -> None:
        headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
        super().__init__(engine, "https://api.anthropic.com/v1/messages", headers)
        self.model = model

    async def complete(self, turns: list[Turn], max_tokens: int, timeout: float, reason: str = "") -> tuple[str, Usage]:
        messages: list[dict[str, Any]] = [{"role": role, "content": content} for role, content in turns[1:]]
        # Turns alternate, so the one before the request is the last worked example; on a retry it is the refused
        # answer, and the examples before it are still read from the cache.
        if len(messages) > 1:
            shared = messages[-2]
            shared["content"] = [{"type": "text", "text": shared["content"], "cache_control": CACHED}]
        body = {
            "model": self.model,
            "max_tokens": max_tokens,
            "temperature": 0,
            "system": [{"type": "text", "text": turns[0][1], "cache_control": CACHED}],
            "messages": messages,
        }
        answer = await self.ask(body, timeout, self.parse)
        return answer

    @staticmethod
    def parse(answer: Any) -> tuple[str, Usage]:
        text = answer["content"][0]["text"]
        counts = answer["usage"]
        # Anthropic counts cached tokens apart from "input_tokens"; Usage counts them as part of the input.
        cached = counts.get("cache_read_input_tokens", 0)
        written = counts.get("cache_creation_input_tokens", 0)
        usage = Usage(counts["input_tokens"] + cached + written, counts["output_tokens"], cached)
        return text, usage
