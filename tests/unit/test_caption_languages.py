"""The caption translation targets (TRX-07, 16-caption-translation LLD, Target languages)."""

from snapbak_backend.ai import languages
from snapbak_backend.transcription.languages import U35_PRO


def test_trx07_source_language_is_not_a_target() -> None:
    assert languages.target("es", "es") is None
    assert languages.target("nb", "no") is None
    assert languages.target("zh-Hans", "zh") is None
    assert languages.target("pt-BR", "pt") is None
    assert languages.target("pt-PT", "pt") == languages.BY_CODE["pt-PT"]
    assert languages.target("es", "en") == languages.BY_CODE["es"]


def test_trx07_an_unknown_code_is_not_a_target() -> None:
    assert languages.target("xx", "en") is None
    assert languages.target("ES", "en") is None


def test_trx07_32_targets_cover_every_transcription_language_once() -> None:
    sources = [language.source for language in languages.LANGUAGES if language.source is not None]
    assert len(languages.LANGUAGES) == 32
    assert sorted(sources) == sorted(U35_PRO)


def test_trx07_source_names_drop_the_variant_and_hinglish_is_both() -> None:
    assert languages.source_name("pt", hinglish=False) == "Portuguese"
    assert languages.source_name("zh", hinglish=False) == "Chinese"
    assert languages.source_name("hi", hinglish=True) == "Hindi and English"
    assert languages.source_name("xx", hinglish=False) == "xx"


def test_trx07_rtl_and_row_limits_follow_the_lld() -> None:
    rtl = {language.code for language in languages.LANGUAGES if language.rtl}
    rows = {language.code: language.row_chars for language in languages.LANGUAGES if language.row_chars != 42}
    assert rtl == {"ar", "he"}
    assert rows == {"zh-Hans": 16, "zh-Hant": 16, "ja": 13, "ko": 16}
