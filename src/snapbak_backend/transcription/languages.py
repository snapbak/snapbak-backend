"""The 18 AssemblyAI Universal-3.5 Pro languages and how a request routes to them (D2, Q22, Q25; docs/cloud.md)."""

from typing import Any

U35_PRO = frozenset(
    {"ar", "da", "nl", "en", "fi", "fr", "de", "he", "hi", "it", "ja", "zh", "no", "pt", "es", "sv", "tr", "vi"}
)
# Only U-3.5 Pro, no Universal-2 fallback, so a language outside the 18 fails instead of being transcribed (D2).
MODEL = {"speech_models": ["universal-3-5-pro"]}


def accepted(language: str) -> bool:
    """`auto`, `hinglish` or one of the 18 codes."""
    return language in ("auto", "hinglish") or language in U35_PRO


def assemblyai_fields(language: str) -> dict[str, Any]:
    """The request fields for a language; `language_codes` and `language_detection` can't go together."""
    if language == "auto":
        return {**MODEL, "language_detection": True}
    if language == "hinglish":
        return {**MODEL, "language_codes": ["en", "hi"]}
    return {**MODEL, "language_code": language}


def base(code: str) -> str:
    """`en_us` is `en`."""
    head, _, _ = code.partition("_")
    return head.lower()
