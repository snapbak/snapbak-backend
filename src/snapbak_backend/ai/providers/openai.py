"""OpenAI and OpenAI-compatible chat completions, such as Groq (docs/providers.md)."""

from typing import Any

from snapbak_backend.ai.providers.base import Http, Turn, Usage

# A call that reasons (a big-tier call or a correction, docs/escalation.md) asks a reasoning model for medium effort:
# on the big tier's 261 of 376 bench rows it passed 150 of the 216 non-English ones against 131 at low effort (round
# b3, D8); others ignore it.
REASONING: dict[str, Any] = {"reasoning_effort": "medium"}


class OpenAICompatible(Http):
    def __init__(self, engine: str, base: str, model: str, key: str, sampling: dict[str, Any]) -> None:
        super().__init__(engine, f"{base}/chat/completions", {"Authorization": f"Bearer {key}"})
        self.model = model
        self.sampling = sampling

    async def complete(
        self, turns: list[Turn], max_tokens: int, timeout: float, reason: bool = False
    ) -> tuple[str, Usage]:
        sampling = self.sampling
        if reason and "reasoning_effort" in sampling:
            sampling = REASONING
        body = {
            "model": self.model,
            **sampling,
            "max_completion_tokens": max_tokens,
            "response_format": {"type": "json_object"},
            "messages": [{"role": role, "content": content} for role, content in turns],
        }
        answer = await self.ask(body, timeout, self.parse)
        return answer

    @staticmethod
    def parse(answer: Any) -> tuple[str, Usage]:
        text = answer["choices"][0]["message"]["content"]
        counts = answer["usage"]
        cached = counts.get("prompt_tokens_details", {}).get("cached_tokens", 0)
        usage = Usage(counts["prompt_tokens"], counts["completion_tokens"], cached)
        return text, usage
