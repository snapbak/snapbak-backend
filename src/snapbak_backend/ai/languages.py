"""The caption translation targets (TRX-07, 16-caption-translation LLD, Target languages): BCP 47 code, English name,
characters per caption row, right to left, and the transcription code (`transcription/languages.py`) a transcript in
that language is detected as, so the transcript's own language is never a target. The app reads a byte-identical
copy, `tests/contract/languages.json` (`payload`)."""

from typing import Any, NamedTuple


class Language(NamedTuple):
    code: str
    name: str
    row_chars: int
    rtl: bool
    source: str | None


LANGUAGES = (
    Language("ar", "Arabic", 42, True, "ar"),
    Language("bn", "Bengali", 42, False, None),
    Language("zh-Hans", "Chinese, Simplified", 16, False, "zh"),
    Language("zh-Hant", "Chinese, Traditional", 16, False, None),
    Language("da", "Danish", 42, False, "da"),
    Language("nl", "Dutch", 42, False, "nl"),
    Language("en", "English", 42, False, "en"),
    Language("fi", "Finnish", 42, False, "fi"),
    Language("fr", "French", 42, False, "fr"),
    Language("de", "German", 42, False, "de"),
    Language("el", "Greek", 42, False, None),
    Language("he", "Hebrew", 42, True, "he"),
    Language("hi", "Hindi", 42, False, "hi"),
    Language("id", "Indonesian", 42, False, None),
    Language("it", "Italian", 42, False, "it"),
    Language("ja", "Japanese", 13, False, "ja"),
    Language("ko", "Korean", 16, False, None),
    Language("ms", "Malay", 42, False, None),
    Language("mr", "Marathi", 42, False, None),
    Language("nb", "Norwegian", 42, False, "no"),
    Language("pl", "Polish", 42, False, None),
    Language("pt-BR", "Portuguese, Brazil", 42, False, "pt"),
    Language("pt-PT", "Portuguese, Portugal", 42, False, None),
    Language("ru", "Russian", 42, False, None),
    Language("es", "Spanish", 42, False, "es"),
    Language("sv", "Swedish", 42, False, "sv"),
    Language("ta", "Tamil", 42, False, None),
    Language("te", "Telugu", 42, False, None),
    Language("th", "Thai", 42, False, None),
    Language("tr", "Turkish", 42, False, "tr"),
    Language("uk", "Ukrainian", 42, False, None),
    Language("vi", "Vietnamese", 42, False, "vi"),
)
BY_CODE = {language.code: language for language in LANGUAGES}
BY_SOURCE = {language.source: language for language in LANGUAGES if language.source is not None}
# The words after the name's language in the system prompt (LLD, Prompt draft).
SCRIPT_NOTES = {"hi": ", Devanagari", "mr": ", Devanagari", "zh-Hant": ", Traditional characters"}
HINGLISH = "Hindi and English"


def target(code: str, source: str) -> Language | None:
    """The target for `code`, or None when it isn't one or is the transcript's own language (`source`, a
    transcription code such as `pt`)."""
    found = BY_CODE.get(code)
    if found is None or found.source == source:
        return None
    return found


def source_name(source: str, hinglish: bool) -> str:
    """The transcript language's English name for the prompt: "Portuguese", not "Portuguese, Brazil"."""
    if hinglish:
        return HINGLISH
    found = BY_SOURCE.get(source)
    if found is None:
        return source
    name, _, _ = found.name.partition(",")
    return name


def payload() -> dict[str, Any]:
    entries = [
        {"code": each.code, "name": each.name, "rowChars": each.row_chars, "rtl": each.rtl, "source": each.source}
        for each in LANGUAGES
    ]
    return {"languages": entries}
