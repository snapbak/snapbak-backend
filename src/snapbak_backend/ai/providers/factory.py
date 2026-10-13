"""A provider from its `provider:model` spec, and each OpenAI model's sampling (docs/providers.md)."""

from collections.abc import Callable, Mapping
from typing import Any

from snapbak_backend.ai.providers.anthropic import Anthropic
from snapbak_backend.ai.providers.base import Provider
from snapbak_backend.ai.providers.fake import Fake
from snapbak_backend.ai.providers.gemini import Gemini
from snapbak_backend.ai.providers.openai import OpenAICompatible
from snapbak_backend.config.settings import KEYS

# OpenAI models that reason run at their lowest effort; the newest refuse temperature 0 (docs/providers.md).
# Any other model gets temperature 0.
TEMPERATURE_0: dict[str, Any] = {"temperature": 0}
NO_REASONING: dict[str, Any] = {**TEMPERATURE_0, "reasoning_effort": "none"}
SAMPLING: dict[str, dict[str, Any]] = {
    "gpt-5-nano": {"reasoning_effort": "minimal"},
    "gpt-5-mini": {"reasoning_effort": "minimal"},
    "gpt-5.1": NO_REASONING,
    "gpt-5.2": NO_REASONING,
    "gpt-5.4": NO_REASONING,
    "gpt-5.4-mini": NO_REASONING,
    "gpt-5.4-nano": NO_REASONING,
    "gpt-5.5": {"reasoning_effort": "none"},
    "gpt-5.6-luna": {"reasoning_effort": "none"},
    "gpt-5.6-terra": {"reasoning_effort": "none"},
    "gpt-5.6-sol": {"reasoning_effort": "none"},
    "gpt-6-luna": {"reasoning_effort": "none"},
    "gpt-6-sol": {"reasoning_effort": "none"},
    "gpt-6-astra": {"reasoning_effort": "low"},
    "gpt-6.1-sol": {"reasoning_effort": "low"},
}


REGISTRY: dict[str, Callable[[str, str], Provider]] = {
    "groq": lambda model, key: OpenAICompatible(
        f"groq:{model}", "https://api.groq.com/openai/v1", model, key, SAMPLING.get(model, TEMPERATURE_0)
    ),
    "openai": lambda model, key: OpenAICompatible(
        f"openai:{model}", "https://api.openai.com/v1", model, key, SAMPLING.get(model, TEMPERATURE_0), fast=True
    ),
    "anthropic": lambda model, key: Anthropic(f"anthropic:{model}", model, key),
    "gemini": lambda model, key: Gemini(f"gemini:{model}", model, key),
    "fake": lambda mode, _key: Fake(mode),
}


def make_provider(spec: str, keys: Mapping[str, str]) -> Provider:
    name, _, model = spec.partition(":")
    if not model:
        raise ValueError(f"Name an engine as provider:model, not '{spec}'.")
    build = REGISTRY.get(name)
    if build is None:
        raise ValueError(f"Unknown provider '{name}'; use one of {sorted(REGISTRY)}.")
    key = keys.get(name, "")
    if name in KEYS and not key:
        raise ValueError(f"Set {KEYS[name]} to use {spec}.")
    return build(model, key)
