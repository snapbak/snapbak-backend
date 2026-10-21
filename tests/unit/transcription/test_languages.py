import pytest

from snapbak_backend.transcription.languages import U35_PRO, accepted, assemblyai_fields, base

MODEL = {"speech_models": ["universal-3-5-pro"]}


def test_trx01_the_18_universal_3_5_pro_languages() -> None:
    assert sorted(U35_PRO) == sorted("ar da nl en fi fr de he hi it ja zh no pt es sv tr vi".split())


@pytest.mark.parametrize(
    ("language", "fields"),
    [
        ("auto", {**MODEL, "language_detection": True}),
        ("hinglish", {**MODEL, "language_codes": ["en", "hi"]}),
        ("hi", {**MODEL, "language_code": "hi"}),
        ("ja", {**MODEL, "language_code": "ja"}),
    ],
)
def test_trx01_each_row_of_the_routing_table(language: str, fields: dict) -> None:
    assert accepted(language)
    assert assemblyai_fields(language) == fields


@pytest.mark.parametrize("language", ["ta", "bn", "", "EN", "en_us", "hinglish "])
def test_trx01_anything_else_is_refused(language: str) -> None:
    assert not accepted(language)


@pytest.mark.parametrize(("code", "expected"), [("en_us", "en"), ("en", "en"), ("hi", "hi"), ("pt_BR", "pt")])
def test_trx01_a_detected_code_is_checked_by_its_base(code: str, expected: str) -> None:
    assert base(code) == expected
