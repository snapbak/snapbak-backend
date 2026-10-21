"""Tests for the marketing site's contact form route (POST /v1/contact)."""

import logging
from typing import Any

import pytest
from conftest import Sql

MESSAGE = {"name": "Ada Lovelace", "email": "Ada@Example.com", "message": "Replay-60 saved my demo."}


def test_contact_stores_the_message_and_thanks_the_sender(api: Any, sql: Sql) -> None:
    response = api.post("/v1/contact", json=MESSAGE)
    assert response.status_code == 201
    assert response.json() == {"message": "Thanks for reaching out. We'll reply soon."}
    assert sql("SELECT name, email, message, source FROM contact_messages") == [
        {"name": "Ada Lovelace", "email": "ada@example.com", "message": "Replay-60 saved my demo.", "source": "website"}
    ]


def test_contact_rejects_a_bad_email_or_an_empty_message(api: Any, sql: Sql) -> None:
    for body in ({**MESSAGE, "email": "not-an-email"}, {**MESSAGE, "message": ""}, {**MESSAGE, "name": ""}):
        response = api.post("/v1/contact", json=body)
        assert (response.status_code, response.json()["error"]["code"]) == (422, "invalid")
    assert sql("SELECT count(*) AS n FROM contact_messages") == [{"n": 0}]


def test_contact_rejects_a_message_over_5000_characters(api: Any) -> None:
    response = api.post("/v1/contact", json={**MESSAGE, "message": "x" * 5001})
    assert response.status_code == 422


def test_contact_never_logs_the_name_email_or_message(api: Any, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    api.post("/v1/contact", json=MESSAGE)
    for word in ("Ada", "Lovelace", "example.com", "Replay-60"):
        assert word not in caplog.text


def test_contact_allows_cors_from_marketing_site(api: Any) -> None:
    headers = {"Origin": "https://snapbak.site", "Access-Control-Request-Method": "POST"}
    response = api.options("/v1/contact", headers=headers)
    assert response.headers["access-control-allow-origin"] == "https://snapbak.site"
