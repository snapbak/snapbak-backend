"""The share caps (Q98): 1 GiB a video, 100 shares a device that are not deleted, one share per recording, also under
concurrent creates (12 LLD)."""

import asyncio
import os
import uuid
from typing import Any

import pytest
from conftest import RECORDING, SHARE, VIDEO_SIZE, Sql, create_share, failure, put_recording, register

from snapbak_backend.config.settings import load
from snapbak_backend.schemas.cloud import ShareRequest
from snapbak_backend.services import shares
from snapbak_backend.services.cloud import open_cloud
from snapbak_backend.services.failures import CloudFailure

GIB = 1_073_741_824


def test_cld03_a_share_over_1_gib_is_refused_before_anything_is_stored(api: Any, sql: Sql) -> None:
    auth = register(api)
    put_recording(api, auth)
    over = create_share(api, auth, bytes=GIB + 1)
    assert failure(over) == (413, "upload_too_large")
    assert sql("SELECT count(*) AS n FROM shares") == [{"n": 0}]
    assert create_share(api, auth, bytes=GIB).status_code == 201


def test_cld03_a_share_video_upload_over_1_gib_is_refused(api: Any, sql: Sql) -> None:
    auth = register(api)
    put_recording(api, auth)
    share = create_share(api, auth).json()["shareId"]
    body = {"kind": "share", "bytes": GIB + 1, "contentType": "video/mp4", "shareId": share, **VIDEO_SIZE}
    response = api.post(f"/v1/recordings/{RECORDING}/uploads", json=body, headers=auth)
    assert failure(response) == (413, "upload_too_large")
    assert sql("SELECT count(*) AS n FROM uploads") == [{"n": 0}]


def shares_of_device(sql: Sql, auth: dict[str, str], api: Any, count: int) -> None:
    """`count` recordings of this device, each with a disabled share, written straight to the database."""
    anchor = str(uuid.uuid4())
    put_recording(api, auth, anchor)
    device = sql("SELECT device_id FROM recordings WHERE id = $1", uuid.UUID(anchor))[0]["device_id"]
    sql(
        "WITH made AS (INSERT INTO recordings (id, device_id, title, duration_ms, created_at)"
        " SELECT gen_random_uuid(), $1, 't', 1, now() FROM generate_series(1, $2) RETURNING id)"
        " INSERT INTO shares (id, recording_id, slug, title, state)"
        " SELECT gen_random_uuid(), id, 'slug-of-share-' || lpad((row_number() OVER () - 1)::text, 4, '0'), 't',"
        " 'disabled' FROM made",
        device,
        count,
    )


def test_cld03_the_101st_share_of_a_device_is_refused_and_a_deleted_one_frees_a_place(api: Any, sql: Sql) -> None:
    auth = register(api)
    shares_of_device(sql, auth, api, 100)
    put_recording(api, auth)
    refused = create_share(api, auth)
    assert (refused.status_code, refused.json()["error"]["code"]) == (429, "share_limit")
    assert refused.json()["error"]["message"] == "This device has 100 share links; delete one to share again."
    sql("UPDATE shares SET deleted_at = now() WHERE slug = 'slug-of-share-0000'")
    assert create_share(api, auth).status_code == 201


def test_cld03_another_devices_shares_do_not_count(api: Any, sql: Sql) -> None:
    other = register(api, str(uuid.uuid4()))
    shares_of_device(sql, other, api, 100)
    auth = register(api)
    put_recording(api, auth)
    assert create_share(api, auth).status_code == 201


async def create(device: uuid.UUID, recording: uuid.UUID) -> uuid.UUID | int:
    """One create in its own session and connection: the share's ID, or the error's status."""
    whole = load(os.environ)
    config = whole.cloud
    assert config is not None
    cloud = await open_cloud(config, whole.keys)
    try:
        async with cloud.sessions() as db:
            try:
                share, _ = await shares.open_share(db, device, recording, ShareRequest.model_validate(SHARE))
            except CloudFailure as error:
                return error.status
            await db.commit()
            return share.id
    finally:
        await cloud.close()


async def together(device: uuid.UUID, recording: uuid.UUID, count: int) -> list[uuid.UUID | int]:
    answers = await asyncio.gather(*(create(device, recording) for _ in range(count)))
    return list(answers)


def test_shr01_concurrent_creates_for_one_recording_make_one_share(api: Any, sql: Sql) -> None:
    auth = register(api)
    put_recording(api, auth)
    device = sql("SELECT device_id FROM recordings WHERE id = $1", uuid.UUID(RECORDING))[0]["device_id"]
    answers = asyncio.run(together(device, uuid.UUID(RECORDING), 8))
    assert len(set(answers)) == 1
    assert sql("SELECT count(*) AS n FROM shares") == [{"n": 1}]


def test_shr01_a_slug_taken_by_chance_is_drawn_again(api: Any, sql: Sql, monkeypatch: pytest.MonkeyPatch) -> None:
    auth = register(api)
    shares_of_device(sql, auth, api, 1)
    put_recording(api, auth)
    drawn = iter(["slug-of-share-0000", "slug-of-share-0000", "Vb3kQx9_mT2LpZaR"])
    monkeypatch.setattr(shares, "new_slug", lambda: next(drawn))
    made = create_share(api, auth)
    assert (made.status_code, made.json()["slug"]) == (201, "Vb3kQx9_mT2LpZaR")


def test_shr01_three_taken_slugs_in_a_row_are_an_internal_error(
    api: Any, sql: Sql, monkeypatch: pytest.MonkeyPatch
) -> None:
    auth = register(api)
    shares_of_device(sql, auth, api, 1)
    put_recording(api, auth)
    drawn: list[str] = []

    def taken() -> str:
        drawn.append("slug-of-share-0000")
        return drawn[-1]

    monkeypatch.setattr(shares, "new_slug", taken)
    assert failure(create_share(api, auth)) == (500, "internal")
    assert len(drawn) == 3
    assert sql("SELECT count(*) AS n FROM shares") == [{"n": 1}]


def test_cld03_a_share_video_up_to_exactly_1_gib_may_start_its_upload(api: Any) -> None:
    auth = register(api)
    put_recording(api, auth)
    share = create_share(api, auth).json()["shareId"]
    for size in (500_000_000, GIB):
        body = {"kind": "share", "bytes": size, "contentType": "video/mp4", "shareId": share, **VIDEO_SIZE}
        response = api.post(f"/v1/recordings/{RECORDING}/uploads", json=body, headers=auth)
        assert response.status_code == 201, size


def test_cld03_two_creates_on_two_recordings_at_99_shares_make_exactly_one(
    api: Any, sql: Sql, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The count spans the device's recordings, so creates of one device take turns on its row: the second counts
    only after the first committed."""
    import threading
    from concurrent.futures import ThreadPoolExecutor

    auth = register(api)
    shares_of_device(sql, auth, api, 99)
    second = str(uuid.uuid4())
    put_recording(api, auth)
    put_recording(api, auth, second)
    real = shares.live_count
    entered = threading.Event()

    async def slow(*args: Any) -> int:
        counted = await real(*args)
        entered.set()
        await asyncio.sleep(0.3)
        return counted

    monkeypatch.setattr(shares, "live_count", slow)
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(create_share, api, auth)
        assert entered.wait(10)
        other = pool.submit(create_share, api, auth, second)
        statuses = sorted([first.result().status_code, other.result().status_code])
    assert statuses == [201, 429]
    assert sql("SELECT count(*) AS n FROM shares WHERE deleted_at IS NULL") == [{"n": 100}]


def test_shr01_a_share_create_beside_the_days_first_generate_waits_its_turn(
    api: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The generate's first minutes of the day insert the device's usage row, whose key check reads the device's
    row; the share create's device lock must not block that check (FOR NO KEY UPDATE), or the two deadlock."""
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from conftest import create_job

    from snapbak_backend.services import usage

    auth = register(api)
    put_recording(api, auth)
    real = usage.reserve
    entered = threading.Event()

    async def slow(*args: Any) -> bool:
        entered.set()
        await asyncio.sleep(0.3)
        answer = await real(*args)
        return answer

    monkeypatch.setattr(usage, "reserve", slow)
    with ThreadPoolExecutor(2) as pool:
        generated = pool.submit(create_job, api, auth)
        assert entered.wait(10)
        made = pool.submit(create_share, api, auth)
        assert [generated.result().status_code, made.result().status_code] == [201, 201]
