"""A share's video and poster through S3: the multipart upload with `kind: "share"`, complete pointing the share
at each new version, the superseded objects tagged, and the races between re-exports (12 LLD)."""

import threading
import uuid
from typing import Any

import httpx
from conftest import RECORDING, VIDEO_SIZE, Sql, create_share, failure, put_recording, register, s3, upload_share

VIDEO_HEADERS = ("video/mp4", "public, max-age=31536000, immutable")


def bucket(api: Any) -> str:
    return api.app.state.cloud.config.bucket


def tags(api: Any, key: str) -> list[dict[str, str]]:
    return s3().get_object_tagging(Bucket=bucket(api), Key=key)["TagSet"]


def put_poster(made: dict[str, Any], data: bytes = b"jpeg") -> int:
    return httpx.put(made["posterUploadUrl"], content=data, headers={"Content-Type": "image/jpeg"}).status_code


def share_row(sql: Sql) -> dict[str, Any]:
    return sql("SELECT state, version, s3_key, poster_s3_key, video_bytes, duration_ms, width, height FROM shares")[0]


def test_shr01_the_first_video_makes_the_share_live_at_v1(api: Any, sql: Sql) -> None:
    auth = register(api)
    put_recording(api, auth)
    made = create_share(api, auth).json()
    share = made["shareId"]
    assert put_poster(made) == 200
    upload_id, done = upload_share(api, auth, share, b"mp4 video bytes")
    assert done.status_code == 200
    assert done.json() == {
        "uploadId": upload_id,
        "state": "complete",
        "share": {"shareId": share, "slug": made["slug"], "url": made["url"], "state": "live", "version": 1},
    }
    assert share_row(sql) == {
        "state": "live",
        "version": 1,
        "s3_key": f"shares/{share}/v1/video.mp4",
        "poster_s3_key": f"shares/{share}/v1/poster.jpg",
        "video_bytes": 15,
        "duration_ms": 61_200,
        "width": 1920,
        "height": 1080,
    }
    video = s3().head_object(Bucket=bucket(api), Key=f"shares/{share}/v1/video.mp4")
    assert (video["ContentType"], video["CacheControl"]) == VIDEO_HEADERS
    assert sql("SELECT kind, share_id FROM uploads") == [{"kind": "share", "share_id": uuid.UUID(share)}]


def test_shr01_a_re_export_moves_to_v2_and_tags_v1_superseded(api: Any, sql: Sql) -> None:
    auth = register(api)
    put_recording(api, auth)
    first = create_share(api, auth).json()
    share = first["shareId"]
    put_poster(first)
    upload_share(api, auth, share, b"first video")
    second = create_share(api, auth).json()
    assert f"/shares/{share}/v2/poster.jpg?" in second["posterUploadUrl"]
    put_poster(second)
    _, done = upload_share(api, auth, share, b"second, longer video", width=1080, height=1920, durationMs=9_000)
    assert done.json()["share"]["version"] == 2
    assert share_row(sql) == {
        "state": "live",
        "version": 2,
        "s3_key": f"shares/{share}/v2/video.mp4",
        "poster_s3_key": f"shares/{share}/v2/poster.jpg",
        "video_bytes": 20,
        "duration_ms": 9_000,
        "width": 1080,
        "height": 1920,
    }
    superseded = [{"Key": "superseded", "Value": "true"}]
    assert tags(api, f"shares/{share}/v1/video.mp4") == superseded
    assert tags(api, f"shares/{share}/v1/poster.jpg") == superseded
    assert tags(api, f"shares/{share}/v2/video.mp4") == []


def test_shr01_without_a_poster_put_the_poster_stays_on_the_older_version(api: Any, sql: Sql) -> None:
    auth = register(api)
    put_recording(api, auth)
    first = create_share(api, auth).json()
    share = first["shareId"]
    upload_share(api, auth, share, b"first video")
    assert share_row(sql)["poster_s3_key"] is None
    put_poster(create_share(api, auth).json())
    upload_share(api, auth, share, b"second video")
    create_share(api, auth)
    upload_share(api, auth, share, b"third video")
    row = share_row(sql)
    assert (row["version"], row["poster_s3_key"]) == (3, f"shares/{share}/v2/poster.jpg")
    assert tags(api, f"shares/{share}/v2/poster.jpg") == []


def test_shr01_a_video_keeps_its_own_size_when_a_newer_create_lands_before_it_completes(api: Any, sql: Sql) -> None:
    auth = register(api)
    put_recording(api, auth)
    share = create_share(api, auth).json()["shareId"]
    size = {"durationMs": 4_321, "width": 640, "height": 360}
    body = {"kind": "share", "bytes": 9, "contentType": "video/mp4", "shareId": share, **size}
    started = start_body(api, auth, body).json()
    etag = httpx.put(started["urls"][0]["url"], content=b"version 1").headers["etag"]
    create_share(api, auth, title="Re-exported")
    parts = {"parts": [{"n": 1, "etag": etag}]}
    assert api.post(f"/v1/uploads/{started['uploadId']}/complete", json=parts, headers=auth).status_code == 200
    row = share_row(sql)
    assert (row["version"], row["duration_ms"], row["width"], row["height"]) == (1, 4_321, 640, 360)


def test_shr01_a_late_complete_of_an_older_upload_changes_nothing(api: Any, sql: Sql) -> None:
    auth = register(api)
    put_recording(api, auth)
    share = create_share(api, auth).json()["shareId"]
    older, _ = upload_share(api, auth, share, b"first video")
    upload_share(api, auth, share, b"second video", width=720, height=720, durationMs=3_000)
    sql("UPDATE uploads SET state = 'uploading', completed_at = NULL WHERE id = $1", uuid.UUID(older))
    late = api.post(f"/v1/uploads/{older}/complete", json={"parts": [{"n": 1, "etag": '"x"'}]}, headers=auth)
    assert (late.status_code, late.json()["share"]["version"]) == (200, 2)
    row = share_row(sql)
    assert (row["s3_key"], row["width"], row["duration_ms"]) == (f"shares/{share}/v2/video.mp4", 720, 3_000)
    assert tags(api, f"shares/{share}/v2/video.mp4") == []


def test_shr01_a_retried_complete_answers_the_share_again(api: Any) -> None:
    auth = register(api)
    put_recording(api, auth)
    share = create_share(api, auth).json()["shareId"]
    upload_id, done = upload_share(api, auth, share, b"video")
    again = api.post(f"/v1/uploads/{upload_id}/complete", json={"parts": [{"n": 1, "etag": "x"}]}, headers=auth)
    assert (again.status_code, again.json()) == (200, done.json())


def test_shr01_a_disabled_share_stays_disabled_when_its_video_completes(api: Any, sql: Sql) -> None:
    auth = register(api)
    put_recording(api, auth)
    made = create_share(api, auth).json()
    api.patch(f"/v1/shares/{made['slug']}", json={"state": "disabled"}, headers=auth)
    _, done = upload_share(api, auth, made["shareId"], b"video")
    assert done.json()["share"]["state"] == "disabled"
    assert (share_row(sql)["state"], share_row(sql)["version"]) == ("disabled", 1)


def start(api: Any, auth: dict[str, str], share: str, size: int = 10) -> Any:
    body = {"kind": "share", "bytes": size, "contentType": "video/mp4", "shareId": share, **VIDEO_SIZE}
    answer = start_body(api, auth, body)
    return answer


def start_body(api: Any, auth: dict[str, str], body: dict[str, Any]) -> Any:
    return api.post(f"/v1/recordings/{RECORDING}/uploads", json=body, headers=auth)


def test_shr01_a_second_upload_aborts_the_first_so_the_later_re_export_wins(api: Any, sql: Sql) -> None:
    auth = register(api)
    put_recording(api, auth)
    share = create_share(api, auth).json()["shareId"]
    first = start(api, auth, share).json()
    etag = httpx.put(first["urls"][0]["url"], content=b"ten bytes!").headers["etag"]
    second = start(api, auth, share).json()
    late = api.post(f"/v1/uploads/{first['uploadId']}/complete", json={"parts": [{"n": 1, "etag": etag}]}, headers=auth)
    assert failure(late) == (409, "upload_state")
    assert sql("SELECT id, state FROM uploads ORDER BY created_at") == [
        {"id": uuid.UUID(first["uploadId"]), "state": "aborted"},
        {"id": uuid.UUID(second["uploadId"]), "state": "uploading"},
    ]
    still_open = [upload["UploadId"] for upload in s3().list_multipart_uploads(Bucket=bucket(api))["Uploads"]]
    assert still_open == [row["upload_id"] for row in sql("SELECT upload_id FROM uploads WHERE state = 'uploading'")]


def test_shr01_parts_of_a_share_upload_are_signed_under_shares(api: Any) -> None:
    auth = register(api)
    put_recording(api, auth)
    share = create_share(api, auth).json()["shareId"]
    started = start(api, auth, share, 20 * 1024 * 1024).json()
    fresh = api.post(f"/v1/uploads/{started['uploadId']}/parts", json={"parts": [3]}, headers=auth).json()
    assert [item["n"] for item in fresh["urls"]] == [3]
    assert fresh["urls"][0]["url"].startswith(
        f"https://{bucket(api)}.s3.ap-south-1.amazonaws.com/shares/{share}/v1/video.mp4?"
    )


def test_shr01_a_share_upload_needs_this_recordings_live_share(api: Any) -> None:
    auth = register(api)
    put_recording(api, auth)
    made = create_share(api, auth).json()
    other_recording = str(uuid.uuid4())
    put_recording(api, auth, other_recording)
    theirs = api.post(
        f"/v1/recordings/{other_recording}/uploads",
        json={"kind": "share", "bytes": 10, "contentType": "video/mp4", "shareId": made["shareId"], **VIDEO_SIZE},
        headers=auth,
    )
    unknown = start(api, auth, str(uuid.uuid4()))
    api.delete(f"/v1/shares/{made['slug']}", headers=auth)
    deleted = start(api, auth, made["shareId"])
    assert (failure(theirs), failure(unknown), failure(deleted)) == ((404, "not_found"),) * 3


def test_shr01_a_share_upload_without_its_share_and_size_or_not_mp4_is_invalid(api: Any) -> None:
    auth = register(api)
    put_recording(api, auth)
    share = create_share(api, auth).json()["shareId"]
    good = {"kind": "share", "bytes": 10, "contentType": "video/mp4", "shareId": share, **VIDEO_SIZE}
    bodies = [
        {key: value for key, value in good.items() if key != "shareId"},
        {key: value for key, value in good.items() if key != "width"},
        {**good, "contentType": "video/quicktime"},
        {**good, "kind": "poster"},
        {**good, "width": 0},
        {**good, "height": 16_385},
        {**good, "durationMs": -1},
        {**good, "durationMs": 2**31},
        {**good, "bytes": 0},
        {**good, "width": 16_385},
        {**good, "height": 0},
    ]
    for body in bodies:
        response = api.post(f"/v1/recordings/{RECORDING}/uploads", json=body, headers=auth)
        assert failure(response) == (422, "invalid"), body


def test_shr01_parallel_starts_leave_one_open_upload_and_the_losers_complete_is_409(api: Any, sql: Sql) -> None:
    """Starts take turns on the share's row: the later one sees and aborts the earlier, never shares its key."""
    import asyncio
    from concurrent.futures import ThreadPoolExecutor

    auth = register(api)
    put_recording(api, auth)
    share = create_share(api, auth).json()["shareId"]
    storage = api.app.state.cloud.storage
    real = storage.create_multipart

    async def slow(*args: Any) -> str:
        await asyncio.sleep(0.3)
        answer = await real(*args)
        return answer

    storage.create_multipart = slow
    try:
        with ThreadPoolExecutor(2) as pool:
            started = list(pool.map(lambda _: start(api, auth, share).json(), range(2)))
    finally:
        del storage.create_multipart
    rows = sql("SELECT id, state FROM uploads ORDER BY created_at")
    assert sorted(row["state"] for row in rows) == ["aborted", "uploading"]
    loser = next(str(row["id"]) for row in rows if row["state"] == "aborted")
    url = next(item["urls"][0]["url"] for item in started if item["uploadId"] == loser)
    etag = httpx.put(url, content=b"ten bytes!").headers.get("etag", '"x"')
    late = api.post(f"/v1/uploads/{loser}/complete", json={"parts": [{"n": 1, "etag": etag}]}, headers=auth)
    assert failure(late) == (409, "upload_state")


def test_shr01_a_complete_racing_a_newer_start_is_409_and_never_completes(api: Any, sql: Sql) -> None:
    """A newer start aborts the upload after the complete read it: the complete reads it again under the
    recording's lock and refuses."""
    import threading

    from snapbak_backend.services import recordings

    auth = register(api)
    put_recording(api, auth)
    share = create_share(api, auth).json()["shareId"]
    first = start(api, auth, share).json()
    etag = httpx.put(first["urls"][0]["url"], content=b"ten bytes!").headers["etag"]
    real = recordings.lock

    async def newer_start_lands_first(session: Any, recording_id: Any) -> Any:
        abort = "UPDATE uploads SET state = 'aborted' WHERE id = $1"
        newer = threading.Thread(target=sql, args=(abort, uuid.UUID(first["uploadId"])))
        newer.start()
        newer.join()
        answer = await real(session, recording_id)
        return answer

    recordings.lock = newer_start_lands_first
    try:
        late = api.post(
            f"/v1/uploads/{first['uploadId']}/complete", json={"parts": [{"n": 1, "etag": etag}]}, headers=auth
        )
    finally:
        recordings.lock = real
    assert failure(late) == (409, "upload_state")
    assert sql("SELECT state FROM uploads WHERE id = $1", uuid.UUID(first["uploadId"])) == [{"state": "aborted"}]
    assert share_row(sql)["version"] == 0


def test_shr01_a_retried_complete_of_the_live_version_never_tags_it(api: Any, sql: Sql) -> None:
    auth = register(api)
    put_recording(api, auth)
    share = create_share(api, auth).json()["shareId"]
    upload_share(api, auth, share, b"first video")
    latest, _ = upload_share(api, auth, share, b"second video")
    sql("UPDATE uploads SET state = 'uploading', completed_at = NULL WHERE id = $1", uuid.UUID(latest))
    again = api.post(f"/v1/uploads/{latest}/complete", json={"parts": [{"n": 1, "etag": '"x"'}]}, headers=auth)
    assert (again.status_code, again.json()["share"]["version"]) == (200, 2)
    assert tags(api, f"shares/{share}/v2/video.mp4") == []
    assert tags(api, f"shares/{share}/v1/video.mp4") == [{"Key": "superseded", "Value": "true"}]


def test_shr01_a_retry_tags_what_a_failed_tagging_left_behind(api: Any, sql: Sql) -> None:
    auth = register(api)
    put_recording(api, auth)
    share = create_share(api, auth).json()["shareId"]
    upload_share(api, auth, share, b"first video")
    storage = api.app.state.cloud.storage

    async def down(keys: list[str]) -> None:
        raise RuntimeError("S3 is down")

    storage.supersede = down
    try:
        latest, failed = upload_share(api, auth, share, b"second video")
    finally:
        del storage.supersede
    assert failed.status_code == 500
    assert share_row(sql)["version"] == 2
    assert tags(api, f"shares/{share}/v1/video.mp4") == []
    again = api.post(f"/v1/uploads/{latest}/complete", json={"parts": [{"n": 1, "etag": "x"}]}, headers=auth)
    assert again.status_code == 200
    assert tags(api, f"shares/{share}/v1/video.mp4") == [{"Key": "superseded", "Value": "true"}]
    assert tags(api, f"shares/{share}/v2/video.mp4") == []


def test_shr01_completing_the_live_version_again_changes_nothing_and_tags_nothing(api: Any, sql: Sql) -> None:
    auth = register(api)
    put_recording(api, auth)
    made = create_share(api, auth).json()
    put_poster(made)
    upload_id, _ = upload_share(api, auth, made["shareId"], b"only video")
    before = share_row(sql)
    sql("UPDATE uploads SET state = 'uploading', completed_at = NULL WHERE id = $1", uuid.UUID(upload_id))
    again = api.post(f"/v1/uploads/{upload_id}/complete", json={"parts": [{"n": 1, "etag": '"x"'}]}, headers=auth)
    assert (again.status_code, share_row(sql)) == (200, before)
    assert tags(api, f"shares/{made['shareId']}/v1/video.mp4") == []
    assert tags(api, f"shares/{made['shareId']}/v1/poster.jpg") == []


def slowed(api: Any, name: str) -> tuple[Any, threading.Event]:
    """The shares bucket's `name` call, made 0.3 s slower, and an event set once a request is inside it, so a second
    request is sent only then and lands while the first holds its locks."""
    import asyncio

    storage = api.app.state.cloud.storage
    real = getattr(storage, name)
    entered = threading.Event()

    async def slow(*args: Any) -> Any:
        entered.set()
        await asyncio.sleep(0.3)
        answer = await real(*args)
        return answer

    setattr(storage, name, slow)
    return storage, entered


def test_shr01_a_start_during_a_complete_waits_and_takes_the_next_version(api: Any, sql: Sql) -> None:
    from concurrent.futures import ThreadPoolExecutor

    auth = register(api)
    put_recording(api, auth)
    share = create_share(api, auth).json()["shareId"]
    first = start(api, auth, share).json()
    etag = httpx.put(first["urls"][0]["url"], content=b"ten bytes!").headers["etag"]
    storage, entered = slowed(api, "complete")
    try:
        with ThreadPoolExecutor(2) as pool:
            done = pool.submit(
                api.post,
                f"/v1/uploads/{first['uploadId']}/complete",
                json={"parts": [{"n": 1, "etag": etag}]},
                headers=auth,
            )
            assert entered.wait(10)
            newer = pool.submit(start, api, auth, share)
            answers = (done.result(), newer.result())
    finally:
        del storage.complete
    assert [answer.status_code for answer in answers] == [200, 201]
    assert share_row(sql)["s3_key"] == f"shares/{share}/v1/video.mp4"
    assert sql("SELECT s3_key, state FROM uploads ORDER BY created_at") == [
        {"s3_key": f"shares/{share}/v1/video.mp4", "state": "complete"},
        {"s3_key": f"shares/{share}/v2/video.mp4", "state": "uploading"},
    ]


def test_shr01_two_completes_of_one_upload_adopt_it_once(api: Any, sql: Sql) -> None:
    from concurrent.futures import ThreadPoolExecutor

    auth = register(api)
    put_recording(api, auth)
    share = create_share(api, auth).json()["shareId"]
    upload_share(api, auth, share, b"first video")
    second = start(api, auth, share).json()
    etag = httpx.put(second["urls"][0]["url"], content=b"ten bytes!").headers["etag"]
    complete = f"/v1/uploads/{second['uploadId']}/complete"
    storage, _ = slowed(api, "complete")
    try:
        with ThreadPoolExecutor(2) as pool:
            calls = [
                pool.submit(api.post, complete, json={"parts": [{"n": 1, "etag": etag}]}, headers=auth)
                for _ in range(2)
            ]
            answers = [call.result() for call in calls]
    finally:
        del storage.complete
    assert [(answer.status_code, answer.json()["share"]["version"]) for answer in answers] == [(200, 2), (200, 2)]
    assert share_row(sql)["s3_key"] == f"shares/{share}/v2/video.mp4"
    assert tags(api, f"shares/{share}/v1/video.mp4") == [{"Key": "superseded", "Value": "true"}]
    assert tags(api, f"shares/{share}/v2/video.mp4") == []


def race_with_delete(api: Any, auth: dict[str, str], first: Any, entered: threading.Event) -> list[int]:
    """`first()` started, then the recording trashed once it is inside S3; both answers' statuses."""
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(2) as pool:
        running = pool.submit(first)
        assert entered.wait(10)
        trashed = pool.submit(api.delete, f"/v1/recordings/{RECORDING}", headers=auth)
        statuses = [running.result().status_code, trashed.result().status_code]
    return statuses


def test_cld04_trashing_a_recording_while_its_share_video_completes_waits_its_turn(api: Any, sql: Sql) -> None:
    """The delete locks the share first, as start and complete do, so the two never deadlock."""
    auth = register(api)
    put_recording(api, auth)
    share = create_share(api, auth).json()["shareId"]
    first = start(api, auth, share).json()
    etag = httpx.put(first["urls"][0]["url"], content=b"ten bytes!").headers["etag"]
    complete = f"/v1/uploads/{first['uploadId']}/complete"
    storage, entered = slowed(api, "complete")
    try:
        statuses = race_with_delete(
            api, auth, lambda: api.post(complete, json={"parts": [{"n": 1, "etag": etag}]}, headers=auth), entered
        )
    finally:
        del storage.complete
    assert statuses == [200, 204]
    assert sql("SELECT state, version, deleted_at IS NOT NULL AS gone FROM shares") == [
        {"state": "disabled", "version": 1, "gone": True}
    ]


def test_cld04_trashing_a_recording_while_its_share_video_starts_waits_its_turn(api: Any, sql: Sql) -> None:
    auth = register(api)
    put_recording(api, auth)
    share = create_share(api, auth).json()["shareId"]
    start(api, auth, share)
    storage, entered = slowed(api, "create_multipart")
    try:
        statuses = race_with_delete(api, auth, lambda: start(api, auth, share), entered)
    finally:
        del storage.create_multipart
    assert statuses == [201, 204]
    assert sql("SELECT state, deleted_at IS NOT NULL AS gone FROM uploads ORDER BY created_at") == [
        {"state": "aborted", "gone": True},
        {"state": "uploading", "gone": True},
    ]


def test_shr01_re_exporting_one_share_never_tags_another_shares_objects(api: Any) -> None:
    auth = register(api)
    put_recording(api, auth)
    other_recording = str(uuid.uuid4())
    put_recording(api, auth, other_recording)
    mine = create_share(api, auth).json()["shareId"]
    other = create_share(api, auth, recording=other_recording).json()["shareId"]
    upload_share(api, auth, other, b"the other video", recording=other_recording)
    upload_share(api, auth, mine, b"first video")
    upload_share(api, auth, mine, b"second video")
    assert tags(api, f"shares/{mine}/v1/video.mp4") == [{"Key": "superseded", "Value": "true"}]
    assert tags(api, f"shares/{other}/v1/video.mp4") == []


def test_shr01_a_new_shares_upload_never_aborts_a_deleted_shares_open_upload(api: Any, sql: Sql) -> None:
    auth = register(api)
    put_recording(api, auth)
    old = create_share(api, auth).json()
    left = start(api, auth, old["shareId"]).json()
    api.delete(f"/v1/shares/{old['slug']}", headers=auth)
    new = create_share(api, auth).json()["shareId"]
    start(api, auth, new)
    assert sql("SELECT state FROM uploads WHERE id = $1", uuid.UUID(left["uploadId"])) == [{"state": "uploading"}]


def test_shr01_a_complete_after_its_share_was_deleted_is_not_found_and_changes_nothing(api: Any, sql: Sql) -> None:
    auth = register(api)
    put_recording(api, auth)
    made = create_share(api, auth).json()
    first = start(api, auth, made["shareId"]).json()
    etag = httpx.put(first["urls"][0]["url"], content=b"ten bytes!").headers["etag"]
    api.delete(f"/v1/shares/{made['slug']}", headers=auth)
    late = api.post(f"/v1/uploads/{first['uploadId']}/complete", json={"parts": [{"n": 1, "etag": etag}]}, headers=auth)
    assert failure(late) == (404, "not_found")
    assert (share_row(sql)["version"], share_row(sql)["s3_key"]) == (0, None)


def test_shr01_a_retried_complete_never_tags_the_next_versions_poster_put_before_its_video(api: Any, sql: Sql) -> None:
    auth = register(api)
    put_recording(api, auth)
    share = create_share(api, auth).json()["shareId"]
    live, _ = upload_share(api, auth, share, b"first video")
    put_poster(create_share(api, auth).json())
    sql("UPDATE uploads SET state = 'uploading', completed_at = NULL WHERE id = $1", uuid.UUID(live))
    again = api.post(f"/v1/uploads/{live}/complete", json={"parts": [{"n": 1, "etag": '"x"'}]}, headers=auth)
    assert again.status_code == 200
    assert tags(api, f"shares/{share}/v2/poster.jpg") == []


def test_shr01_turning_a_share_on_while_its_first_video_completes_waits_and_succeeds(api: Any, sql: Sql) -> None:
    """The PATCH waits for the complete's lock, then reads the share again: it sees version 1, not the stale 0."""
    from concurrent.futures import ThreadPoolExecutor

    auth = register(api)
    put_recording(api, auth)
    made = create_share(api, auth).json()
    first = start(api, auth, made["shareId"]).json()
    etag = httpx.put(first["urls"][0]["url"], content=b"ten bytes!").headers["etag"]
    api.patch(f"/v1/shares/{made['slug']}", json={"state": "disabled"}, headers=auth)
    storage, entered = slowed(api, "complete")
    try:
        with ThreadPoolExecutor(2) as pool:
            done = pool.submit(
                api.post,
                f"/v1/uploads/{first['uploadId']}/complete",
                json={"parts": [{"n": 1, "etag": etag}]},
                headers=auth,
            )
            assert entered.wait(10)
            on = pool.submit(api.patch, f"/v1/shares/{made['slug']}", json={"state": "live"}, headers=auth)
            statuses = [done.result().status_code, on.result().status_code]
    finally:
        del storage.complete
    assert statuses == [200, 200]
    assert (share_row(sql)["state"], share_row(sql)["version"]) == ("live", 1)
