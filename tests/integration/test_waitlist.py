"""Tests for the waitlist routes (GET /v1/waitlist/status, POST /v1/waitlist, POST /v1/waitlist/claim)."""

from typing import Any

from conftest import Sql

TOKEN = "Bearer waitlist-token"


def test_waitlist_status_creates_default_spots(api: Any, sql: Sql) -> None:
    response = api.get("/v1/waitlist/status")
    assert response.status_code == 200
    body = response.json()
    assert body == {"spotsTotal": 100, "spotsClaimed": 0, "waitlistOpen": True}
    assert sql("SELECT total_spots, claimed_spots FROM early_access_spots") == [
        {"total_spots": 100, "claimed_spots": 0}
    ]


def test_waitlist_status_returns_existing_spots(api: Any, sql: Sql) -> None:
    sql("INSERT INTO early_access_spots (id, total_spots, claimed_spots) VALUES (1, 50, 25)")
    response = api.get("/v1/waitlist/status")
    assert response.status_code == 200
    assert response.json() == {"spotsTotal": 50, "spotsClaimed": 25, "waitlistOpen": True}


def test_waitlist_status_shows_closed_when_full(api: Any, sql: Sql) -> None:
    sql("INSERT INTO early_access_spots (id, total_spots, claimed_spots) VALUES (1, 10, 10)")
    response = api.get("/v1/waitlist/status")
    assert response.status_code == 200
    assert response.json() == {"spotsTotal": 10, "spotsClaimed": 10, "waitlistOpen": False}


def test_waitlist_signup_stores_email_and_returns_success(api: Any, sql: Sql) -> None:
    response = api.post("/v1/waitlist", json={"email": "hello@example.com", "useCase": "Testing", "source": "hero"})
    assert response.status_code == 201
    assert response.json() == {"message": "Thanks for joining the Snapbak waitlist."}
    rows = sql("SELECT email, use_case, source FROM waitlist_entries")
    assert rows == [{"email": "hello@example.com", "use_case": "Testing", "source": "hero"}]


def test_waitlist_signup_is_idempotent(api: Any, sql: Sql) -> None:
    first = api.post("/v1/waitlist", json={"email": "Dup@Example.com", "useCase": "First"})
    second = api.post("/v1/waitlist", json={"email": "dup@example.com", "useCase": "Second"})
    assert first.status_code == 201
    assert second.status_code == 201
    rows = sql("SELECT email, use_case, source FROM waitlist_entries")
    assert rows == [{"email": "dup@example.com", "use_case": "First", "source": "website"}]


def test_waitlist_signup_rejects_invalid_email(api: Any) -> None:
    response = api.post("/v1/waitlist", json={"email": "not-an-email"})
    assert (response.status_code, response.json()["error"]["code"]) == (422, "invalid")


def test_waitlist_claim_increments_claimed_spots(api: Any, sql: Sql) -> None:
    api.get("/v1/waitlist/status")
    response = api.post("/v1/waitlist/claim", headers={"Authorization": TOKEN})
    assert response.status_code == 200
    assert response.json() == {"spotsTotal": 100, "spotsClaimed": 1, "waitlistOpen": True}
    assert sql("SELECT claimed_spots FROM early_access_spots") == [{"claimed_spots": 1}]


def test_waitlist_claim_does_not_exceed_total(api: Any, sql: Sql) -> None:
    sql("INSERT INTO early_access_spots (id, total_spots, claimed_spots) VALUES (1, 2, 2)")
    response = api.post("/v1/waitlist/claim", headers={"Authorization": TOKEN})
    assert response.status_code == 200
    assert response.json() == {"spotsTotal": 2, "spotsClaimed": 2, "waitlistOpen": False}


def test_waitlist_claim_rejects_missing_or_bad_token(api: Any) -> None:
    for headers in ({}, {"Authorization": "Bearer wrong"}, {"Authorization": "Token waitlist-token"}):
        response = api.post("/v1/waitlist/claim", headers=headers)
        assert (response.status_code, response.json()["error"]["code"]) == (401, "unauthorized")


def test_waitlist_routes_allow_cors_from_marketing_site(api: Any) -> None:
    headers = {"Origin": "https://snapbak.site"}
    response = api.get("/v1/waitlist/status", headers=headers)
    assert response.headers["access-control-allow-origin"] == "https://snapbak.site"
