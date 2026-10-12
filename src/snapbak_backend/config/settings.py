"""Server settings from the environment (docs/providers.md)."""

from collections.abc import Mapping
from dataclasses import dataclass

KEYS = {
    "groq": "GROQ_API_KEY",
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "gemini": "GEMINI_API_KEY",
}


@dataclass(frozen=True)
class Config:
    token: str
    small: str
    big: str
    fallback: str | None
    keys: dict[str, str]


def required(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, "")
    if not value:
        raise ValueError(f"Set {name} in the environment (see .env.example).")
    return value


def load(env: Mapping[str, str]) -> Config:
    keys = {provider: env.get(name, "") for provider, name in KEYS.items()}
    return Config(
        token=required(env, "SNAPBAK_TOKEN"),
        small=required(env, "SNAPBAK_SMALL"),
        big=required(env, "SNAPBAK_BIG"),
        fallback=env.get("SNAPBAK_FALLBACK") or None,
        keys=keys,
    )
