import json
import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from snapbak_backend import main as app_module
from snapbak_backend.api.v1 import polish as polish_api
from snapbak_backend.schemas.command import CommandResponse
from snapbak_backend.schemas.polish import PolishResponse

CONTRACT = Path(__file__).parent.parent / "contract"
POLISH = json.loads((CONTRACT / "polish.request.json").read_text())
COMMAND = json.loads((CONTRACT / "command.request.json").read_text())
AUTH = {"Authorization": "Bearer dev"}
ENV = {
    "SNAPBAK_TOKEN": "dev",
    "SNAPBAK_SMALL": "fake:edit",
    "SNAPBAK_BIG": "fake:edit",
    "SNAPBAK_FALLBACK": "",
}


@contextmanager
def serve(monkeypatch: pytest.MonkeyPatch, **env: str) -> Iterator[TestClient]:
    for name, value in {**ENV, **env}.items():
        monkeypatch.setenv(name, value)
    with TestClient(app_module.app, raise_server_exceptions=False) as client:
        yield client


def chunk(text: str, **changes: object) -> dict:
    return {**POLISH, "text": text, "prior": "", **changes}


def test_healthz_answers_without_a_token(monkeypatch: pytest.MonkeyPatch) -> None:
    with serve(monkeypatch) as client:
        response = client.get("/healthz")
    assert (response.status_code, response.json()) == (200, {"ok": True})


def test_healthz_warms_each_provider_connection_at_most_every_half_minute(monkeypatch: pytest.MonkeyPatch) -> None:
    models = {"SNAPBAK_SMALL": "openai:gpt-4.1-nano", "SNAPBAK_BIG": "openai:gpt-4.1", "OPENAI_API_KEY": "sk-test"}
    with respx.mock() as mock, serve(monkeypatch, **models) as client:
        warmed = mock.head("https://api.openai.com/v1/chat/completions").respond(405)
        client.get("/healthz")
        first = warmed.call_count
        client.get("/healthz")
    assert (first, warmed.call_count) == (2, 2)


def test_healthz_answers_when_warming_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    models = {"SNAPBAK_SMALL": "openai:gpt-4.1-nano", "SNAPBAK_BIG": "openai:gpt-4.1", "OPENAI_API_KEY": "sk-test"}
    with respx.mock() as mock, serve(monkeypatch, **models) as client:
        mock.head("https://api.openai.com/v1/chat/completions").mock(side_effect=httpx.ConnectError("down"))
        response = client.get("/healthz")
    assert (response.status_code, response.json()) == (200, {"ok": True})


def test_polish_answers_the_contract_request(monkeypatch: pytest.MonkeyPatch) -> None:
    with serve(monkeypatch) as client:
        response = client.post("/v1/polish", json=POLISH, headers=AUTH)
    assert response.status_code == 200
    body = PolishResponse.model_validate(response.json())
    # The fake echoes the piece: it keeps "no wait" with no revision, a kept correction, so the text goes in as said.
    assert (body.text, body.revision, body.rejected) == ("no wait make it thursday not friday", None, True)
    assert (body.engine, body.tier, body.tokens) == ("fake:edit", "big", 0)


@pytest.mark.parametrize(
    ("prior", "text", "revision", "expected"),
    [
        ("Hi team. Okay so we ship it today!", "No wait, Friday.", "Hi team.", "Okay so we ship it Friday!"),
        ("Okay so we ship it today!", "no wait friday", "", "Okay so we ship it friday!"),
        ("रिपोर्ट तैयार है। मीटिंग सोमवार।", "no wait मंगलवार", "रिपोर्ट तैयार है।", "मीटिंग मंगलवार।"),
    ],
)
def test_fake_edit_corrects_the_last_word_of_the_prior_after_no_wait(
    monkeypatch: pytest.MonkeyPatch, prior: str, text: str, revision: str, expected: str
) -> None:
    with serve(monkeypatch) as client:
        response = client.post("/v1/polish", json=chunk(text, prior=prior), headers=AUTH)
    body = PolishResponse.model_validate(response.json())
    assert (body.text, body.revision, body.rejected) == (expected, revision, False)


@pytest.mark.parametrize(
    ("text", "language", "expected", "tier"),
    [
        ("ship the build tonight", "en", "ship the build tonight!", "small"),
        ("aaj build ship karte hain", "hinglish", "aaj build ship karte hain!", "big"),
        ("आज बिल्ड भेजते हैं।", "hi", "आज बिल्ड भेजते हैं!", "big"),
    ],
)
def test_polish_routes_each_language_to_its_tier(
    monkeypatch: pytest.MonkeyPatch, text: str, language: str, expected: str, tier: str
) -> None:
    with serve(monkeypatch) as client:
        response = client.post("/v1/polish", json=chunk(text, language=language), headers=AUTH)
    assert (response.json()["text"], response.json()["tier"]) == (expected, tier)


def test_command_answers_the_contract_request_on_the_big_tier(monkeypatch: pytest.MonkeyPatch) -> None:
    with serve(monkeypatch, SNAPBAK_SMALL="fake:fail") as client:
        response = client.post("/v1/command", json=COMMAND, headers=AUTH)
    assert response.status_code == 200
    body = CommandResponse.model_validate(response.json())
    assert (body.text, body.engine) == ("HEY CAN U SEND THE DECK BY EOD", "fake:edit")


@pytest.mark.parametrize(
    "headers", [{}, {"Authorization": "Bearer nope"}, {"Authorization": "dev"}, {"Authorization": "Bearer "}]
)
@pytest.mark.parametrize(("path", "body"), [("/v1/polish", POLISH), ("/v1/command", COMMAND), ("/v1/polish", {})])
def test_a_missing_or_wrong_token_is_unauthorized(
    monkeypatch: pytest.MonkeyPatch, headers: dict, path: str, body: dict
) -> None:
    with serve(monkeypatch) as client:
        response = client.post(path, json=body, headers=headers)
    assert (response.status_code, response.json()) == (401, {"error": "unauthorized"})


def test_the_error_body_matches_the_contract_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    with serve(monkeypatch) as client:
        response = client.post("/v1/polish", json=POLISH)
    assert response.json() == json.loads((CONTRACT / "error.response.json").read_text())


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/v1/polish", {}),
        ("/v1/polish", chunk("")),
        ("/v1/polish", chunk("ship it", deadline_ms=299)),
        ("/v1/polish", chunk("ship it", style="tweet")),
        ("/v1/polish", chunk("ship it", terms=["x"] * 201)),
        ("/v1/command", {**COMMAND, "selection": ""}),
        ("/v1/command", {**COMMAND, "instruction": "a" * 2001}),
    ],
)
def test_a_request_outside_the_contract_is_invalid(monkeypatch: pytest.MonkeyPatch, path: str, body: dict) -> None:
    with serve(monkeypatch) as client:
        response = client.post(path, json=body, headers=AUTH)
    assert (response.status_code, response.json()) == (422, {"error": "invalid"})


def test_a_body_that_is_not_json_is_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    with serve(monkeypatch) as client:
        response = client.post("/v1/polish", content=b"{nope", headers={**AUTH, "Content-Type": "application/json"})
    assert (response.status_code, response.json()) == (422, {"error": "invalid"})


@pytest.mark.parametrize(("path", "body"), [("/v1/polish", POLISH), ("/v1/command", COMMAND)])
def test_every_provider_failing_is_a_provider_error(monkeypatch: pytest.MonkeyPatch, path: str, body: dict) -> None:
    with serve(monkeypatch, SNAPBAK_SMALL="fake:fail", SNAPBAK_BIG="fake:fail", SNAPBAK_FALLBACK="fake:fail") as client:
        response = client.post(path, json=body, headers=AUTH)
    assert (response.status_code, response.json()) == (502, {"error": "provider"})


def test_a_failing_tier_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    with serve(monkeypatch, SNAPBAK_BIG="fake:fail", SNAPBAK_FALLBACK="fake:echo") as client:
        response = client.post("/v1/polish", json=POLISH, headers=AUTH)
    assert (response.status_code, response.json()["engine"]) == (200, "fake:echo")
    assert response.json()["text"] == POLISH["text"]


def test_a_slow_provider_answers_502_by_the_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    with serve(monkeypatch, SNAPBAK_BIG="fake:slow") as client:
        started = time.monotonic()
        response = client.post("/v1/polish", json={**POLISH, "deadline_ms": 500}, headers=AUTH)
        took = time.monotonic() - started
    assert (response.status_code, response.json()) == (502, {"error": "provider"})
    assert took < 0.8


def test_a_rejected_small_answer_is_retried_on_the_big_tier(monkeypatch: pytest.MonkeyPatch) -> None:
    reply = json.dumps({"revision": None, "text": "Sure! Here is your message: ship the build tonight."})
    answer = {
        "choices": [{"message": {"content": reply}}],
        "usage": {"prompt_tokens": 900, "completion_tokens": 20},
    }
    with (
        respx.mock(assert_all_called=True) as mock,
        serve(monkeypatch, SNAPBAK_SMALL="openai:gpt-4.1-nano", OPENAI_API_KEY="sk-test") as client,
    ):
        mock.post("https://api.openai.com/v1/chat/completions").respond(json=answer)
        response = client.post("/v1/polish", json=chunk("ship the build tonight"), headers=AUTH)
    body = response.json()
    assert (body["text"], body["tier"], body["engine"], body["rejected"]) == (
        "ship the build tonight!",
        "big",
        "fake:edit",
        False,
    )
    assert body["tokens"] == 920


def test_an_unexpected_error_is_internal(monkeypatch: pytest.MonkeyPatch) -> None:
    async def broken(*_: object) -> None:
        raise RuntimeError("bug")

    monkeypatch.setattr(polish_api, "polish", broken)
    with serve(monkeypatch) as client:
        response = client.post("/v1/polish", json=POLISH, headers=AUTH)
    assert (response.status_code, response.json()) == (500, {"error": "internal"})


def test_the_server_refuses_to_start_without_a_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SNAPBAK_TOKEN", raising=False)
    monkeypatch.setenv("SNAPBAK_SMALL", "fake:edit")
    monkeypatch.setenv("SNAPBAK_BIG", "fake:edit")
    with pytest.raises(ValueError, match="SNAPBAK_TOKEN"), TestClient(app_module.app):
        pass


SECRET = "zebra quokka narwhal"
CASES = [
    ({}, chunk(f"ship {SECRET}"), 200, "verdict=ok"),
    ({"SNAPBAK_SMALL": "fake:echo"}, chunk(f"Sure {SECRET}", prior=f"{SECRET}."), 200, "verdict=ok"),
    (
        {"SNAPBAK_SMALL": "fake:fail", "SNAPBAK_BIG": "fake:fail"},
        chunk(f"ship {SECRET}"),
        502,
        "provider=fake model=fail tier=- ms=",
    ),
    (
        {"SNAPBAK_SMALL": "fake:slow", "SNAPBAK_FALLBACK": "fake:echo"},
        chunk(f"ship {SECRET}", deadline_ms=400),
        502,
        "provider=fake model=slow tier=- ms=",
    ),
    ({}, chunk(f"ship {SECRET}", style="tweet"), 422, "verdict=invalid"),
]


@pytest.mark.parametrize(("env", "body", "status", "verdict"), CASES)
def test_one_log_line_per_request_and_never_any_text(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    env: dict,
    body: dict,
    status: int,
    verdict: str,
) -> None:
    caplog.set_level(logging.DEBUG)
    with serve(monkeypatch, **env) as client:
        caplog.clear()
        response = client.post("/v1/polish", json=body, headers=AUTH)
    lines = [record.getMessage() for record in caplog.records if record.name == "snapbak_backend"]
    assert response.status_code == status
    assert len(lines) == 1
    assert lines[0].startswith("route=/v1/polish provider=")
    assert verdict in lines[0]
    if status == 502:
        expected = "verdict=timeout" if "slow" in lines[0] else "verdict=error"
        # Each line ends with the 429 and 5xx answers asked again, none here (D6).
        assert lines[0].endswith(f"{expected} retries=0"), lines[0]
    for word in SECRET.split():
        assert word not in caplog.text


def test_a_rejection_is_logged_with_its_reason_and_no_text(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    reply = json.dumps({"revision": None, "text": f"Sure, {SECRET} and more."})
    answer = {
        "choices": [{"message": {"content": reply}}],
        "usage": {"prompt_tokens": 900, "completion_tokens": 20},
    }
    caplog.set_level(logging.DEBUG)
    with (
        respx.mock() as mock,
        serve(
            monkeypatch,
            SNAPBAK_SMALL="openai:gpt-4.1-nano",
            SNAPBAK_BIG="openai:gpt-4.1",
            OPENAI_API_KEY="sk-test",
        ) as client,
    ):
        mock.post("https://api.openai.com/v1/chat/completions").respond(json=answer)
        caplog.clear()
        response = client.post("/v1/polish", json=chunk(f"ship {SECRET}"), headers=AUTH)
    assert response.json()["rejected"] is True
    lines = [record.getMessage() for record in caplog.records if record.name == "snapbak_backend"]
    assert lines == [lines[0]]
    assert "provider=openai model=gpt-4.1 tier=big" in lines[0]
    assert "verdict=reply>reply" in lines[0]
    for word in SECRET.split():
        assert word not in caplog.text
