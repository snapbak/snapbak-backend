"""The price table the bench and replay use (docs/providers.md)."""

# US$ per million (input, output) tokens, list prices; check them before trusting a cost report. OpenAI's are the
# Standard tier from developers.openai.com/api/docs/pricing, read 10 Oct 2026.
PRICES: dict[str, tuple[float, float]] = {
    "groq:llama-3.1-8b-instant": (0.05, 0.08),
    "groq:llama-3.3-70b-versatile": (0.59, 0.79),
    "groq:openai/gpt-oss-20b": (0.075, 0.30),
    "groq:openai/gpt-oss-120b": (0.15, 0.60),
    "openai:gpt-4.1-nano": (0.10, 0.40),
    "openai:gpt-4.1-mini": (0.40, 1.60),
    "openai:gpt-4.1": (2.00, 8.00),
    "openai:gpt-5-nano": (0.05, 0.40),
    "openai:gpt-5-mini": (0.25, 2.00),
    "openai:gpt-5.1": (1.25, 10.00),
    "openai:gpt-5.2": (1.75, 14.00),
    "openai:gpt-5.4-nano": (0.20, 1.25),
    "openai:gpt-5.4-mini": (0.75, 4.50),
    "openai:gpt-5.4": (2.50, 15.00),
    "openai:gpt-5.5": (5.00, 30.00),
    "openai:gpt-5.6-luna": (0.20, 1.20),
    "openai:gpt-5.6-terra": (2.00, 12.00),
    "openai:gpt-5.6-sol": (4.00, 20.00),
    "openai:gpt-6-luna": (0.10, 0.50),
    "openai:gpt-6-sol": (2.00, 10.00),
    "openai:gpt-6-astra": (10.00, 50.00),
    "openai:gpt-6.1-sol": (2.00, 10.00),
    "anthropic:claude-haiku-4-5": (1.00, 5.00),
    "gemini:gemini-2.5-flash-lite": (0.10, 0.40),
    "gemini:gemini-2.5-flash": (0.30, 2.50),
}


# US$ per million input tokens served from the provider's prompt cache; an engine not here bills them as input.
CACHED: dict[str, float] = {
    "openai:gpt-4.1-nano": 0.025,
    "openai:gpt-4.1-mini": 0.10,
    "openai:gpt-4.1": 0.50,
    "openai:gpt-5-nano": 0.005,
    "openai:gpt-5-mini": 0.025,
    "openai:gpt-5.1": 0.125,
    "openai:gpt-5.2": 0.175,
    "openai:gpt-5.4-nano": 0.02,
    "openai:gpt-5.4-mini": 0.075,
    "openai:gpt-5.4": 0.25,
    "openai:gpt-5.5": 0.50,
    "openai:gpt-5.6-luna": 0.02,
    "openai:gpt-5.6-terra": 0.20,
    "openai:gpt-5.6-sol": 0.40,
    "openai:gpt-6-luna": 0.01,
    "openai:gpt-6-sol": 0.20,
    "openai:gpt-6-astra": 1.00,
    "openai:gpt-6.1-sol": 0.10,
}


def cost(engine: str, input_tokens: int, output_tokens: int, cached_tokens: int = 0) -> float:
    """`cached_tokens` are the part of `input_tokens` the provider served from its prompt cache."""
    per_input, per_output = PRICES.get(engine, (0.0, 0.0))
    per_cached = CACHED.get(engine, per_input)
    fresh = input_tokens - cached_tokens
    dollars = (fresh * per_input + cached_tokens * per_cached + output_tokens * per_output) / 1_000_000
    return dollars
