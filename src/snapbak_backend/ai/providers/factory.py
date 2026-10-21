"""A provider from its `provider:model` spec, and each OpenAI model's sampling (docs/providers.md)."""

from collections.abc import Callable, Mapping
from typing import Any

from snapbak_backend.ai.chain import Tiers
from snapbak_backend.ai.providers.anthropic import Anthropic
from snapbak_backend.ai.providers.base import Provider
from snapbak_backend.ai.providers.fake import Fake
from snapbak_backend.ai.providers.gemini import Gemini
from snapbak_backend.ai.providers.openai import OpenAICompatible
from snapbak_backend.config.settings import KEYS, Config

# OpenAI models that reason run at their lowest effort; the newest refuse temperature 0 (docs/providers.md).
# Any other model gets temperature 0, or the polish temperature it is made with, and a fixed seed.
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


SEED = 7


def sampled(model: str, temperature: float) -> dict[str, Any]:
    """A model's sampling at this temperature, with a fixed seed, where it takes a temperature at all."""
    sampling = SAMPLING.get(model, TEMPERATURE_0)
    if "temperature" not in sampling:
        return sampling
    tempered = {**sampling, "temperature": temperature, "seed": SEED}
    return tempered


# Each provider from its model, key and temperature; only the OpenAI-style ones take the temperature.
REGISTRY: dict[str, Callable[[str, str, float], Provider]] = {
    "groq": lambda model, key, heat: OpenAICompatible(
        f"groq:{model}", "https://api.groq.com/openai/v1", model, key, sampled(model, heat)
    ),
    "openai": lambda model, key, heat: OpenAICompatible(
        f"openai:{model}", "https://api.openai.com/v1", model, key, sampled(model, heat), fast=True
    ),
    "anthropic": lambda model, key, _heat: Anthropic(f"anthropic:{model}", model, key),
    "gemini": lambda model, key, _heat: Gemini(f"gemini:{model}", model, key),
    "fake": lambda mode, _key, _heat: Fake(mode),
}


def make_provider(spec: str, keys: Mapping[str, str], temperature: float = 0.0) -> Provider:
    name, _, model = spec.partition(":")
    if not model:
        raise ValueError(f"Name an engine as provider:model, not '{spec}'.")
    build = REGISTRY.get(name)
    if build is None:
        raise ValueError(f"Unknown provider '{name}'; use one of {sorted(REGISTRY)}.")
    key = keys.get(name, "")
    if name in KEYS and not key:
        raise ValueError(f"Set {KEYS[name]} to use {spec}.")
    provider = build(model, key, temperature)
    return provider


def make_tiers(config: Config) -> Tiers:
    """Every model the settings name, built once per process: by the API at startup and by the worker."""
    fallback = make_provider(config.fallback, config.keys) if config.fallback else None
    correction = make_provider(config.correction, config.keys) if config.correction else None
    translate = make_provider(config.translate, config.keys) if config.translate else None
    tiers = Tiers(
        polish=make_provider(config.polish, config.keys, config.temperature),
        command=make_provider(config.big, config.keys),
        fallback=fallback,
        correction=correction,
        effort=config.effort,
        examples=config.examples,
        translate=translate,
    )
    return tiers
