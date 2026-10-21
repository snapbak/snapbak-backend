import threading
import uuid
from typing import Any

import pytest
from conftest import RECORDING, Sql, create_job, failure, put_recording, register, s3, upload_audio

BODY = {"title": "Recording 2026-10-17 at 10.02.11", "durationMs": 612_400, "createdAt": "2026-10-17T04:32:11Z"}


def test_cld02_put_creates_the_recording_then_updates_it(api: Any, sql: Sql) -> None:
    auth = register(api)
    created = put_recording(api, auth)
    updated = api.put(f"/v1/recordings/{RECORDING}", json={**BODY, "title": "Demo", "durationMs": 5}, headers=auth)
    assert (created.status_code, created.json()) == (
        201,
        {"recordingId": RECORDING, "title": "Recording 2026-10-17 at 10.02.11", "durationMs": 612_400},
    )
    assert (updated.status_code, updated.json()) == (200, {"recordingId": RECORDING, "title": "Demo", "durationMs": 5})
    assert sql("SELECT title, duration_ms FROM recordings") == [{"title": "Demo", "duration_ms": 5}]


def test_cld02_another_devices_recording_is_not_found_never_forbidden(api: Any) -> None:
    put_recording(api, register(api))
    other = register(api, str(uuid.uuid4()))
    response = put_recording(api, other)
    assert (response.status_code, response.json()["error"]["code"]) == (404, "not_found")
    assert failure(api.delete(f"/v1/recordings/{RECORDING}", headers=other)) == (404, "not_found")


def test_cld02_bad_recording_bodies_are_invalid(api: Any) -> None:
    auth = register(api)
    for body in ({**BODY, "title": "x" * 301}, {**BODY, "durationMs": -1}, {**BODY, "createdAt": "yesterday"}):
        response = api.put(f"/v1/recordings/{RECORDING}", json=body, headers=auth)
        assert (response.status_code, response.json()["error"]["code"]) == (422, "invalid")


def test_cld04_delete_soft_deletes_the_recording_and_everything_under_it(api: Any, sql: Sql) -> None:
    auth = register(api)
    put_recording(api, auth)
    speaker, share = uuid.uuid4(), uuid.uuid4()
    sql(
        "INSERT INTO speakers (id, recording_id, label, vendor_label, color_idx) VALUES ($1, $2, 'Speaker 1', 'S0', 0)",
        speaker,
        uuid.UUID(RECORDING),
    )
    sql(
        "INSERT INTO transcript_lines (id, recording_id, speaker_id, idx, start_ms, end_ms, text, words)"
        " VALUES ($1, $2, $3, 0, 0, 1, 'Hi', '[]')",
        uuid.uuid4(),
        uuid.UUID(RECORDING),
        speaker,
    )
    sql(
        "INSERT INTO shares (id, recording_id, slug, title, state) VALUES ($1, $2, 'abcdefghijklmnop', 't', 'live')",
        share,
        uuid.UUID(RECORDING),
    )
    sql(
        "INSERT INTO caption_tracks (id, recording_id, language, lines) VALUES ($1, $2, 'fr', '[]')",
        uuid.uuid4(),
        uuid.UUID(RECORDING),
    )
    create_job(api, auth)
    upload_id, _ = upload_audio(api, auth, b"m4a")
    first = api.delete(f"/v1/recordings/{RECORDING}", headers=auth)
    second = api.delete(f"/v1/recordings/{RECORDING}", headers=auth)
    assert (first.status_code, second.status_code) == (204, 204)
    tables = ("recordings", "uploads", "transcription_jobs", "speakers", "transcript_lines", "shares", "caption_tracks")
    for table in tables:
        # Soft forever (Q99, Q101): every row stays, marked.
        rows = sql(f"SELECT count(*) AS n, count(deleted_at) AS marked FROM {table}")
        assert rows == [{"n": 1, "marked": 1}], table
    assert sql("SELECT state FROM shares") == [{"state": "disabled"}]
    # The audio stays in S3 too (Q15).
    bucket = api.app.state.cloud.config.bucket
    assert s3().head_object(Bucket=bucket, Key=f"audio/{RECORDING}/{upload_id}.m4a")["ContentLength"] == 3
    assert failure(put_recording(api, auth)) == (404, "not_found")


def test_cld04_deleting_a_recording_before_its_audio_arrives_fails_its_job_and_refunds_now(api: Any, sql: Sql) -> None:
    auth = register(api)
    put_recording(api, auth)
    create_job(api, auth)
    assert sql("SELECT audio_seconds FROM usage_daily") == [{"audio_seconds": 613}]
    assert api.delete(f"/v1/recordings/{RECORDING}", headers=auth).status_code == 204
    assert sql("SELECT state, failure_reason, version FROM transcription_jobs") == [
        {"state": "failed", "failure_reason": "internal", "version": 2}
    ]
    assert sql("SELECT audio_seconds FROM usage_daily") == [{"audio_seconds": 0}]


def slowed_service(monkeypatch: Any, module: Any, name: str, when: Any = lambda *a, **k: True) -> threading.Event:
    """`module.name` made 0.3 s slower whenever `when(*args, **kwargs)` holds; the event is set once a request is
    inside it, so a racing request is sent only then."""
    import asyncio

    real = getattr(module, name)
    entered = threading.Event()

    async def slow(*args: Any, **kwargs: Any) -> Any:
        if when(*args, **kwargs):
            entered.set()
            await asyncio.sleep(0.3)
        answer = await real(*args, **kwargs)
        return answer

    monkeypatch.setattr(module, name, slow)
    return entered


def raced(first: Any, then: Any, entered: threading.Event) -> list[int]:
    """`first()`, then `then()` once the first is inside its slowed step; both statuses."""
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(2) as pool:
        running = pool.submit(first)
        assert entered.wait(10)
        second = pool.submit(then)
        statuses = [running.result().status_code, second.result().status_code]
    return statuses


def test_cld04_trashing_a_recording_while_its_audio_completes_waits_its_turn(api: Any, monkeypatch: Any) -> None:
    """The complete holds the recording's lock while it moves the upload and starts the job; the delete waits for
    that lock, then trashes both."""
    import httpx

    from snapbak_backend.services import jobs

    auth = register(api)
    put_recording(api, auth)
    create_job(api, auth)
    body = {"kind": "audio", "bytes": 3, "contentType": "audio/mp4"}
    started = api.post(f"/v1/recordings/{RECORDING}/uploads", json=body, headers=auth).json()
    etag = httpx.put(started["urls"][0]["url"], content=b"m4a").headers["etag"]
    entered = slowed_service(monkeypatch, jobs, "advance", lambda *args, **kwargs: "upload_id" in kwargs)
    statuses = raced(
        lambda: api.post(
            f"/v1/uploads/{started['uploadId']}/complete", json={"parts": [{"n": 1, "etag": etag}]}, headers=auth
        ),
        lambda: api.delete(f"/v1/recordings/{RECORDING}", headers=auth),
        entered,
    )
    assert statuses == [200, 204]


@pytest.mark.parametrize("first", ["generate", "delete"])
def test_cld04_a_generate_racing_the_trash_of_its_recording_never_leaves_a_queued_job(
    api: Any, sql: Sql, monkeypatch: Any, first: str
) -> None:
    """A failed earlier job and its audio, so the generate is a retry that queues at once. Whichever goes first, the
    other waits on the recording's row: a generate first is then failed by the delete; a delete first makes the
    generate 404."""
    from snapbak_backend.services import retention, usage

    auth = register(api)
    put_recording(api, auth)
    create_job(api, auth)
    upload_audio(api, auth, b"m4a")
    sql("UPDATE transcription_jobs SET state = 'failed', failure_reason = 'vendor_error', version = version + 1")

    def generate() -> Any:
        return create_job(api, auth)

    def trash() -> Any:
        return api.delete(f"/v1/recordings/{RECORDING}", headers=auth)

    if first == "generate":
        entered = slowed_service(monkeypatch, usage, "reserve")
        assert raced(generate, trash, entered) == [201, 204]
    else:
        entered = slowed_service(monkeypatch, retention, "on_recording_deleted")
        assert raced(trash, generate, entered) == [204, 404]
    jobs = sql("SELECT state, deleted_at IS NOT NULL AS gone FROM transcription_jobs ORDER BY created_at")
    assert all(job["state"] == "failed" and job["gone"] for job in jobs), jobs
    assert len(jobs) == (2 if first == "generate" else 1)
