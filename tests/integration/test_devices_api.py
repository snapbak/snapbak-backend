import hashlib
import json
import re
import uuid
from pathlib import Path
from typing import Any

from conftest import INSTALL, Sql, failure, register

CODE = {"error": {"code": "unauthorized", "message": "Register this device again."}}


def test_cld01_registering_returns_a_device_and_a_256_bit_token_stored_only_as_its_hash(api: Any, sql: Sql) -> None:
    response = api.post("/v1/devices", json={"installId": INSTALL, "appVersion": "1.0.0"})
    body = response.json()
    assert response.status_code == 201
    assert set(body) == {"deviceId", "token", "voiceprintConsent"}
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", body["token"])
    assert body["voiceprintConsent"] is None
    rows = sql("SELECT id::text, token_hash FROM devices")
    assert rows == [{"id": body["deviceId"], "token_hash": hashlib.sha256(body["token"].encode()).digest()}]


def test_cld01_registering_again_keeps_the_device_and_retires_the_old_token(api: Any, sql: Sql) -> None:
    first = api.post("/v1/devices", json={"installId": INSTALL, "appVersion": "1.0.0"}).json()
    second = api.post("/v1/devices", json={"installId": INSTALL, "appVersion": "1.0.1"}).json()
    assert first["deviceId"] == second["deviceId"]
    old = api.put(
        "/v1/devices/me/consent", json={"voiceprints": True}, headers={"Authorization": f"Bearer {first['token']}"}
    )
    new = api.put(
        "/v1/devices/me/consent", json={"voiceprints": True}, headers={"Authorization": f"Bearer {second['token']}"}
    )
    assert (old.status_code, old.json(), new.status_code) == (401, CODE, 200)
    assert sql("SELECT count(*) AS n FROM devices") == [{"n": 1}]


def test_cld01_a_bad_install_id_is_invalid(api: Any) -> None:
    response = api.post("/v1/devices", json={"installId": "nope", "appVersion": "1.0.0"})
    assert (response.status_code, response.json()["error"]["code"]) == (422, "invalid")


def test_cld01_missing_malformed_and_unknown_tokens_are_unauthorized(api: Any) -> None:
    for headers in ({}, {"Authorization": "Token x"}, {"Authorization": "Bearer unknown"}):
        response = api.put("/v1/devices/me/consent", json={"voiceprints": True}, headers=headers)
        assert (response.status_code, response.json()) == (401, CODE)


def test_cld01_a_deleted_device_is_unauthorized(api: Any, sql: Sql) -> None:
    auth = register(api)
    sql("UPDATE devices SET deleted_at = now()")
    response = api.put("/v1/devices/me/consent", json={"voiceprints": True}, headers=auth)
    assert failure(response) == (401, "unauthorized")


def test_trx08_consent_is_stored_with_its_time_and_can_be_withdrawn(api: Any, sql: Sql) -> None:
    auth = register(api)
    allowed = api.put("/v1/devices/me/consent", json={"voiceprints": True}, headers=auth)
    assert allowed.status_code == 200
    assert allowed.json()["voiceprintConsent"] is True
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", allowed.json()["consentAt"])
    withdrawn = api.put("/v1/devices/me/consent", json={"voiceprints": False}, headers=auth).json()
    assert withdrawn["voiceprintConsent"] is False
    assert sql("SELECT voiceprint_consent FROM devices") == [{"voiceprint_consent": False}]
    again = api.post("/v1/devices", json={"installId": INSTALL, "appVersion": "1.0.0"}).json()
    assert again["voiceprintConsent"] is False


def test_cld01_last_seen_is_written_at_most_once_a_minute(api: Any, sql: Sql) -> None:
    auth = register(api)
    sql("UPDATE devices SET last_seen_at = now() - interval '2 minutes'")
    api.put("/v1/devices/me/consent", json={"voiceprints": True}, headers=auth)
    seen = sql("SELECT now() - last_seen_at < interval '5 seconds' AS fresh FROM devices")
    assert seen == [{"fresh": True}]


def test_cld01_each_cloud_request_logs_one_line_without_the_token(api: Any, caplog: Any) -> None:
    import logging

    with caplog.at_level(logging.INFO, logger="snapbak_backend"):
        token = api.post("/v1/devices", json={"installId": INSTALL, "appVersion": "1.0.0"}).json()["token"]
    lines = [record.getMessage() for record in caplog.records if record.name == "snapbak_backend"]
    assert len(lines) == 1
    assert lines[0].startswith("route=/v1/devices provider=- model=- tier=- ms=")
    assert lines[0].endswith("tokens=0 verdict=201 retries=0")
    assert token not in lines[0]


def test_cld03_one_address_may_register_10_devices_an_hour(api: Any) -> None:
    for _ in range(10):
        made = api.post("/v1/devices", json={"installId": str(uuid.uuid4()), "appVersion": "1.0.0"})
        assert made.status_code == 201
    refused = api.post("/v1/devices", json={"installId": str(uuid.uuid4()), "appVersion": "1.0.0"})
    assert (refused.status_code, refused.json()["error"]["code"]) == (429, "rate_limited")


def test_dic17_polish_and_command_take_a_device_token_so_no_shared_secret_ships_in_the_app(api: Any, sql: Sql) -> None:
    contract = Path(__file__).parent.parent / "contract"
    polish = json.loads((contract / "polish.request.json").read_text())
    command = json.loads((contract / "command.request.json").read_text())
    auth = register(api)
    assert api.post("/v1/polish", json=polish, headers=auth).status_code == 200
    assert api.post("/v1/command", json=command, headers=auth).status_code == 200
    stranger = {"Authorization": "Bearer not-a-device"}
    assert api.post("/v1/polish", json=polish, headers=stranger).status_code == 401
    sql("UPDATE devices SET deleted_at = now()")
    assert api.post("/v1/polish", json=polish, headers=auth).status_code == 401
