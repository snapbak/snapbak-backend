"""The share routes on the compose stack: create, read, turn off and on, expiry and delete (SHR-01, CLD-04; 12 LLD)."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from conftest import RECORDING, SHARE, Sql, create_share, failure, put_recording, register


def near(when: str | None, expected: datetime) -> bool:
    assert when is not None
    return abs(datetime.fromisoformat(when) - expected) < timedelta(seconds=5)


def test_shr01_create_makes_an_uploading_share_with_a_poster_url_for_its_first_version(api: Any, sql: Sql) -> None:
    auth = register(api)
    put_recording(api, auth)
    response = create_share(api, auth)
    body = response.json()
    share = body["shareId"]
    assert response.status_code == 201
    assert len(body["slug"]) == 16
    assert {key: value for key, value in body.items() if key not in ("shareId", "slug", "posterUploadUrl")} == {
        "url": f"http://testserver/v/{body['slug']}",
        "state": "uploading",
        "version": 0,
        "pageState": "uploading",
        "expiresAt": None,
        "type": "video",
        "needsUpload": False,
    }
    assert f"/shares/{share}/v1/poster.jpg?" in body["posterUploadUrl"]
    assert "X-Amz-Signature=" in body["posterUploadUrl"]
    assert sql("SELECT title, state, version, s3_key FROM shares") == [
        {"title": "Recording 2026-10-17 at 10.02.11", "state": "uploading", "version": 0, "s3_key": None}
    ]


def test_shr01_create_again_keeps_the_slug_and_expiry_and_takes_the_new_title_and_size(api: Any, sql: Sql) -> None:
    auth = register(api)
    put_recording(api, auth)
    first = create_share(api, auth).json()
    again = create_share(api, auth, title="Demo, take 2", expiresIn="7d")
    assert again.status_code == 200
    assert (again.json()["shareId"], again.json()["slug"], again.json()["expiresAt"]) == (
        first["shareId"],
        first["slug"],
        None,
    )
    assert sql("SELECT title FROM shares") == [{"title": "Demo, take 2"}]


@pytest.mark.parametrize(("said", "days"), [("7d", 7), ("30d", 30)])
def test_shr01_an_expiry_on_create_counts_from_the_servers_clock(api: Any, said: str, days: int) -> None:
    auth = register(api)
    put_recording(api, auth)
    body = create_share(api, auth, expiresIn=said).json()
    assert near(body["expiresAt"], datetime.now(UTC) + timedelta(days=days))


def test_shr01_another_devices_or_an_unknown_recording_is_not_found(api: Any) -> None:
    put_recording(api, register(api))
    other = register(api, str(uuid.uuid4()))
    assert failure(create_share(api, other)) == (404, "not_found")
    assert failure(create_share(api, other, recording=str(uuid.uuid4()))) == (404, "not_found")
    assert failure(api.get(f"/v1/recordings/{RECORDING}/shares", headers=other)) == (404, "not_found")


@pytest.mark.parametrize(
    "change",
    [{"expiresIn": "1d"}, {"title": "x" * 301}, {"title": ""}, {"bytes": 0}],
)
def test_shr01_a_bad_create_body_is_invalid(api: Any, change: dict[str, Any]) -> None:
    auth = register(api)
    put_recording(api, auth)
    response = api.post(f"/v1/recordings/{RECORDING}/shares", json={**SHARE, **change}, headers=auth)
    assert failure(response) == (422, "invalid")


def test_shr01_get_answers_the_share_without_a_poster_url_and_404_before_one_exists(api: Any) -> None:
    auth = register(api)
    put_recording(api, auth)
    before = api.get(f"/v1/recordings/{RECORDING}/shares", headers=auth)
    made = create_share(api, auth).json()
    after = api.get(f"/v1/recordings/{RECORDING}/shares", headers=auth)
    assert failure(before) == (404, "not_found")
    assert after.status_code == 200
    assert after.json() == {**made, "posterUploadUrl": None}


def test_shr01_patch_turns_a_live_share_off_and_on(api: Any, sql: Sql) -> None:
    auth = register(api)
    put_recording(api, auth)
    slug = create_share(api, auth).json()["slug"]
    sql("UPDATE shares SET state = 'live', version = 1")
    off = api.patch(f"/v1/shares/{slug}", json={"state": "disabled"}, headers=auth)
    on = api.patch(f"/v1/shares/{slug}", json={"state": "live"}, headers=auth)
    assert (off.status_code, off.json()["state"], off.json()["pageState"]) == (200, "disabled", "disabled")
    assert (on.status_code, on.json()["state"], on.json()["pageState"]) == (200, "live", "live")


def test_shr01_an_uploading_share_cannot_be_turned_on_but_can_be_turned_off(api: Any) -> None:
    auth = register(api)
    put_recording(api, auth)
    slug = create_share(api, auth).json()["slug"]
    on = api.patch(f"/v1/shares/{slug}", json={"state": "live"}, headers=auth)
    bad = api.patch(f"/v1/shares/{slug}", json={"state": "uploading"}, headers=auth)
    off = api.patch(f"/v1/shares/{slug}", json={"state": "disabled"}, headers=auth)
    assert (failure(on), failure(bad)) == ((422, "invalid"), (422, "invalid"))
    assert off.json()["state"] == "disabled"


def test_shr01_patch_sets_the_expiry_from_now_and_never_clears_it_and_revives_an_expired_link(
    api: Any, sql: Sql
) -> None:
    auth = register(api)
    put_recording(api, auth)
    slug = create_share(api, auth).json()["slug"]
    sql("UPDATE shares SET state = 'live', version = 1, expires_at = now() - interval '1 day'")
    expired = api.get(f"/v1/recordings/{RECORDING}/shares", headers=auth).json()
    thirty = api.patch(f"/v1/shares/{slug}", json={"expiresIn": "30d"}, headers=auth).json()
    never = api.patch(f"/v1/shares/{slug}", json={"expiresIn": "never"}, headers=auth).json()
    unchanged = api.patch(f"/v1/shares/{slug}", json={}, headers=auth).json()
    assert expired["pageState"] == "expired"
    assert near(thirty["expiresAt"], datetime.now(UTC) + timedelta(days=30))
    assert thirty["pageState"] == "live"
    assert (never["expiresAt"], never["pageState"]) == (None, "live")
    assert unchanged == never


def test_cld04_delete_is_soft_and_idempotent_and_the_next_share_gets_a_new_slug(api: Any, sql: Sql) -> None:
    auth = register(api)
    put_recording(api, auth)
    first = create_share(api, auth).json()
    deleted = api.delete(f"/v1/shares/{first['slug']}", headers=auth)
    stamped = sql("SELECT deleted_at FROM shares")
    again = api.delete(f"/v1/shares/{first['slug']}", headers=auth)
    assert sql("SELECT deleted_at FROM shares") == stamped
    assert (deleted.status_code, again.status_code) == (204, 204)
    assert failure(api.get(f"/v1/recordings/{RECORDING}/shares", headers=auth)) == (404, "not_found")
    assert failure(api.patch(f"/v1/shares/{first['slug']}", json={"state": "live"}, headers=auth)) == (
        404,
        "not_found",
    )
    second = create_share(api, auth)
    assert second.status_code == 201
    assert second.json()["slug"] != first["slug"]
    assert sql("SELECT state, deleted_at IS NOT NULL AS deleted FROM shares ORDER BY created_at") == [
        {"state": "disabled", "deleted": True},
        {"state": "uploading", "deleted": False},
    ]


def test_shr01_another_devices_share_cannot_be_changed_or_deleted(api: Any, sql: Sql) -> None:
    auth = register(api)
    put_recording(api, auth)
    slug = create_share(api, auth).json()["slug"]
    other = register(api, str(uuid.uuid4()))
    patched = api.patch(f"/v1/shares/{slug}", json={"state": "disabled"}, headers=other)
    deleted = api.delete(f"/v1/shares/{slug}", headers=other)
    unknown = api.delete("/v1/shares/AAAAAAAAAAAAAAAA", headers=auth)
    assert (failure(patched), failure(deleted), failure(unknown)) == ((404, "not_found"),) * 3
    assert sql("SELECT state, deleted_at FROM shares") == [{"state": "uploading", "deleted_at": None}]


def test_cld04_a_trashed_recording_takes_its_share_with_it(api: Any) -> None:
    auth = register(api)
    put_recording(api, auth)
    slug = create_share(api, auth).json()["slug"]
    api.delete(f"/v1/recordings/{RECORDING}", headers=auth)
    assert failure(api.patch(f"/v1/shares/{slug}", json={"state": "live"}, headers=auth)) == (404, "not_found")
    assert api.delete(f"/v1/shares/{slug}", headers=auth).status_code == 204


def test_shr01_a_share_turned_off_before_its_first_video_cannot_be_turned_on(api: Any) -> None:
    auth = register(api)
    put_recording(api, auth)
    slug = create_share(api, auth).json()["slug"]
    api.patch(f"/v1/shares/{slug}", json={"state": "disabled"}, headers=auth)
    assert failure(api.patch(f"/v1/shares/{slug}", json={"state": "live"}, headers=auth)) == (422, "invalid")


def test_shr01_a_create_without_expiry_never_expires(api: Any, sql: Sql) -> None:
    auth = register(api)
    put_recording(api, auth)
    body = {key: value for key, value in SHARE.items() if key != "expiresIn"}
    made = api.post(f"/v1/recordings/{RECORDING}/shares", json=body, headers=auth).json()
    assert made["expiresAt"] is None
    assert sql("SELECT expires_at FROM shares") == [{"expires_at": None}]


def test_shr01_the_poster_upload_url_lasts_an_hour(api: Any) -> None:
    from urllib.parse import parse_qs, urlsplit

    auth = register(api)
    put_recording(api, auth)
    url = create_share(api, auth).json()["posterUploadUrl"]
    assert parse_qs(urlsplit(url).query)["X-Amz-Expires"] == ["3600"]


def test_cld04_a_share_created_while_its_recording_is_trashed_is_not_found(
    api: Any, sql: Sql, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from snapbak_backend.services import retention

    auth = register(api)
    put_recording(api, auth)
    real = retention.on_recording_deleted
    entered = threading.Event()

    async def slow(*args: Any) -> None:
        entered.set()
        await asyncio.sleep(0.3)
        await real(*args)

    monkeypatch.setattr(retention, "on_recording_deleted", slow)
    with ThreadPoolExecutor(2) as pool:
        trashed = pool.submit(api.delete, f"/v1/recordings/{RECORDING}", headers=auth)
        assert entered.wait(10)
        made = pool.submit(create_share, api, auth)
        assert [trashed.result().status_code, made.result().status_code] == [204, 404]
    assert sql("SELECT count(*) AS n FROM shares WHERE deleted_at IS NULL") == [{"n": 0}]


def test_shr01_an_expiry_change_on_a_turned_off_share_keeps_it_off(api: Any, sql: Sql) -> None:
    auth = register(api)
    put_recording(api, auth)
    slug = create_share(api, auth).json()["slug"]
    sql("UPDATE shares SET state = 'live', version = 1")
    api.patch(f"/v1/shares/{slug}", json={"state": "disabled"}, headers=auth)
    changed = api.patch(f"/v1/shares/{slug}", json={"expiresIn": "7d"}, headers=auth).json()
    assert (changed["state"], changed["pageState"]) == ("disabled", "disabled")
    assert sql("SELECT state FROM shares") == [{"state": "disabled"}]


def test_shr01_turning_a_share_off_keeps_its_expiry_and_expiry_reads_as_utc(api: Any, sql: Sql) -> None:
    import re

    auth = register(api)
    put_recording(api, auth)
    made = create_share(api, auth, expiresIn="7d").json()
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", made["expiresAt"]), made["expiresAt"]
    stored = sql("SELECT expires_at FROM shares")
    off = api.patch(f"/v1/shares/{made['slug']}", json={"state": "disabled"}, headers=auth).json()
    assert off["expiresAt"] == made["expiresAt"]
    assert sql("SELECT expires_at FROM shares") == stored


def test_shr01_a_bad_expiry_on_patch_is_invalid(api: Any) -> None:
    auth = register(api)
    put_recording(api, auth)
    slug = create_share(api, auth).json()["slug"]
    assert failure(api.patch(f"/v1/shares/{slug}", json={"expiresIn": "1d"}, headers=auth)) == (422, "invalid")
