import json
from pathlib import Path

import pytest
from pydantic import BaseModel

from snapbak_backend.schemas.command import CommandRequest, CommandResponse
from snapbak_backend.schemas.polish import PolishRequest, PolishResponse

CONTRACT = Path(__file__).parent.parent / "contract"
APP_CONTRACT = Path(__file__).parents[3] / "snapbak" / "apps" / "desktop" / "contracts" / "backend"


@pytest.mark.skipif(not APP_CONTRACT.is_dir(), reason="the app repo is not checked out next to this one")
def test_each_fixture_is_byte_identical_to_the_apps() -> None:
    ours = {path.name: path.read_bytes() for path in CONTRACT.glob("*.json")}
    apps = {path.name: path.read_bytes() for path in APP_CONTRACT.glob("*.json")}
    assert ours == apps


@pytest.mark.parametrize(
    ("fixture", "model"),
    [
        ("polish.request.json", PolishRequest),
        ("polish.response.json", PolishResponse),
        ("command.request.json", CommandRequest),
        ("command.response.json", CommandResponse),
    ],
)
def test_each_fixture_is_exactly_its_model(fixture: str, model: type[BaseModel]) -> None:
    data = json.loads((CONTRACT / fixture).read_text())
    parsed = model.model_validate(data)
    assert parsed.model_dump() == data
    assert set(data) == set(model.model_fields)


def test_the_error_fixture_is_one_code() -> None:
    data = json.loads((CONTRACT / "error.response.json").read_text())
    assert data == {"error": "unauthorized"}
