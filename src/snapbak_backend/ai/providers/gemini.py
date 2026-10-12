"""Gemini generateContent (docs/providers.md)."""

from typing import Any

from snapbak_backend.ai.providers.base import Http, Turn, Usage


class Gemini(Http):
    def __init__(self, engine: str, model: str, key: str) -> None:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        super().__init__(engine, url, {"x-goog-api-key": key})

    async def complete(
        self, turns: list[Turn], max_tokens: int, timeout: float, reason: bool = False
    ) -> tuple[str, Usage]:
        body = {
            "systemInstruction": {"parts": [{"text": turns[0][1]}]},
            "contents": [
                {"role": "model" if role == "assistant" else "user", "parts": [{"text": content}]}
                for role, content in turns[1:]
            ],
            "generationConfig": {
                "temperature": 0,
                "maxOutputTokens": max_tokens,
                "responseMimeType": "application/json",
                "thinkingConfig": {"thinkingBudget": 0},
            },
        }
        answer = await self.ask(body, timeout, self.parse)
        return answer

    @staticmethod
    def parse(answer: Any) -> tuple[str, Usage]:
        text = answer["candidates"][0]["content"]["parts"][0]["text"]
        metadata = answer["usageMetadata"]
        usage = Usage(metadata["promptTokenCount"], metadata["candidatesTokenCount"])
        return text, usage
