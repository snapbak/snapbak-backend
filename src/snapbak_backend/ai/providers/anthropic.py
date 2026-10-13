"""Anthropic messages (docs/providers.md)."""

from typing import Any

from snapbak_backend.ai.providers.base import Http, Turn, Usage


class Anthropic(Http):
    def __init__(self, engine: str, model: str, key: str) -> None:
        headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
        super().__init__(engine, "https://api.anthropic.com/v1/messages", headers)
        self.model = model

    async def complete(self, turns: list[Turn], max_tokens: int, timeout: float, reason: str = "") -> tuple[str, Usage]:
        body = {
            "model": self.model,
            "max_tokens": max_tokens,
            "temperature": 0,
            "system": turns[0][1],
            "messages": [{"role": role, "content": content} for role, content in turns[1:]],
        }
        answer = await self.ask(body, timeout, self.parse)
        return answer

    @staticmethod
    def parse(answer: Any) -> tuple[str, Usage]:
        text = answer["content"][0]["text"]
        usage = Usage(answer["usage"]["input_tokens"], answer["usage"]["output_tokens"])
        return text, usage
