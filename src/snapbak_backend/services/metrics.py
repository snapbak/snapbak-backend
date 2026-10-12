"""The one log line per request: counts only, never text (CLAUDE.md rule 3)."""

import logging

from snapbak_backend.ai.chain import Call

logger = logging.getLogger("snapbak_backend")


def log(route: str, engine: str, tier: str, ms: int, tokens: int, verdict: str, retried: int = 0) -> None:
    provider, _, model = engine.partition(":")
    logger.info(
        "route=%s provider=%s model=%s tier=%s ms=%d tokens=%d verdict=%s retries=%d",
        route,
        provider,
        model or "-",
        tier,
        ms,
        tokens,
        verdict,
        retried,
    )


def verdicts(calls: list[Call]) -> str:
    return ">".join(call.verdict for call in calls)
