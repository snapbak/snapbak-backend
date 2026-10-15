import pytest
from pydantic import ValidationError

from snapbak_backend.schemas.command import CommandRequest
from snapbak_backend.schemas.polish import PolishRequest

POLISH = {
    "text": "ship it",
    "prior": "",
    "terms": [],
    "app": "",
    "style": "other",
    "deadline_ms": 2000,
}
COMMAND = {"selection": "hey", "instruction": "formal", "app": "", "deadline_ms": 6000}


@pytest.mark.parametrize(
    "changes",
    [
        {"text": "a" * 8000},
        {"text": "", "prior": "Let's meet on Friday."},
        {"prior": " ".join(["word"] * 300)},
        {"terms": ["x"] * 200},
        {"terms": ["a" * 100, "GMMCO"]},
        {"app": "a" * 100},
        {"style": "email"},
        {"deadline_ms": 300},
        # The app waits up to 20 s for the AI: accuracy over speed (D2, 11 Oct).
        {"deadline_ms": 20_000},
    ],
)
def test_polish_requests_at_the_limits_are_valid(changes: dict) -> None:
    PolishRequest.model_validate({**POLISH, **changes})


@pytest.mark.parametrize(
    "changes",
    [
        {"text": "a" * 8001},
        {"text": ""},
        {"prior": " ".join(["word"] * 301)},
        {"terms": ["x"] * 201},
        {"terms": [""]},
        {"terms": ["a" * 101]},
        {"app": "a" * 101},
        {"style": "tweet"},
        {"deadline_ms": 299},
        {"deadline_ms": 20_001},
        {"text": None},
    ],
)
def test_polish_requests_past_the_limits_are_invalid(changes: dict) -> None:
    with pytest.raises(ValidationError):
        PolishRequest.model_validate({**POLISH, **changes})


@pytest.mark.parametrize(("model", "fields"), [(PolishRequest, POLISH), (CommandRequest, COMMAND)])
def test_requests_carry_no_language_and_an_older_apps_language_is_dropped(model: type, fields: dict) -> None:
    # Snapbak is English only (founder, 14 Oct 2026): the field left the contract.
    request = model.model_validate({**fields, "language": "en"})
    assert "language" not in request.model_dump()


def test_continues_is_false_unless_the_app_says_the_cursor_is_mid_sentence() -> None:
    assert PolishRequest.model_validate(POLISH).continues is False
    assert PolishRequest.model_validate({**POLISH, "continues": True}).continues is True


@pytest.mark.parametrize("missing", list(POLISH))
def test_every_polish_field_is_required(missing: str) -> None:
    fields = {name: value for name, value in POLISH.items() if name != missing}
    with pytest.raises(ValidationError):
        PolishRequest.model_validate(fields)


@pytest.mark.parametrize(
    "changes",
    [{"selection": "a" * 20_000}, {"instruction": "a" * 2_000}, {"selection": "x", "instruction": "y"}],
)
def test_command_requests_at_the_limits_are_valid(changes: dict) -> None:
    CommandRequest.model_validate({**COMMAND, **changes})


@pytest.mark.parametrize(
    "changes",
    [
        {"selection": ""},
        {"selection": "a" * 20_001},
        {"instruction": ""},
        {"instruction": "a" * 2_001},
        {"app": "a" * 101},
        {"deadline_ms": 10_001},
    ],
)
def test_command_requests_past_the_limits_are_invalid(changes: dict) -> None:
    with pytest.raises(ValidationError):
        CommandRequest.model_validate({**COMMAND, **changes})
