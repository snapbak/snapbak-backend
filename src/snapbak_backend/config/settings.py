"""Server settings from the environment (docs/providers.md)."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, get_args

KEYS = {
    "groq": "GROQ_API_KEY",
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "gemini": "GEMINI_API_KEY",
}
# How many worked examples go before a polish request: every one (as before 15 Oct 2026), a few per flagged prompt
# module, or none (docs/api.md).
type ExamplesPolicy = Literal["all", "per_module", "none"]
# The one polish model and its effort, the R7 round's winner (15 Oct 2026, docs/bench.md).
POLISH = "openai:gpt-6-luna"
EFFORT = "none"
POLICIES: tuple[ExamplesPolicy, ...] = get_args(ExamplesPolicy.__value__)


@dataclass(frozen=True)
class Config:
    token: str
    polish: str
    big: str
    correction: str | None
    fallback: str | None
    temperature: float
    effort: str
    examples: ExamplesPolicy
    keys: dict[str, str]


def required(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, "")
    if not value:
        raise ValueError(f"Set {name} in the environment (see .env.example).")
    return value


def temperature(env: Mapping[str, str]) -> float:
    said = env.get("SNAPBAK_POLISH_TEMPERATURE") or "0"
    try:
        value = float(said)
    except ValueError:
        value = -1.0
    if not 0 <= value <= 2:
        raise ValueError(f"Set SNAPBAK_POLISH_TEMPERATURE to a number from 0 to 2, not '{said}'.")
    return value


def examples(env: Mapping[str, str]) -> ExamplesPolicy:
    said = env.get("SNAPBAK_EXAMPLES_POLICY") or "all"
    policy: ExamplesPolicy | None = next((known for known in POLICIES if known == said), None)
    if policy is None:
        raise ValueError(f"Set SNAPBAK_EXAMPLES_POLICY to one of {', '.join(POLICIES)}, not '{said}'.")
    return policy


def load(env: Mapping[str, str]) -> Config:
    keys = {provider: env.get(name, "") for provider, name in KEYS.items()}
    big = required(env, "SNAPBAK_BIG")
    # A refused answer is corrected once, on the big model unless another is named; "none" turns the retry off.
    correction = env.get("SNAPBAK_CORRECTION") or big
    return Config(
        token=required(env, "SNAPBAK_TOKEN"),
        polish=env.get("SNAPBAK_POLISH") or POLISH,
        big=big,
        correction=None if correction == "none" else correction,
        fallback=env.get("SNAPBAK_FALLBACK") or None,
        temperature=temperature(env),
        effort=env.get("SNAPBAK_POLISH_EFFORT") or EFFORT,
        examples=examples(env),
        keys=keys,
    )
