"""Writes racing the trash of their recording: each takes the recording's lock first, so it waits for the delete, then
answers 404 and writes nothing; or, if it goes first, finishes before the delete starts (docs/cloud.md, Locks)."""

import asyncio
import os
import threading
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from conftest import RECORDING, Sql, admin_on, create_share, eventually, failure, put_recording, register

from snapbak_backend.config.settings import load
from snapbak_backend.services import retention, transliteration
from snapbak_backend.services.cloud import open_cloud

SPEAKER = uuid.UUID("2b7c1e0a-1111-4c2d-9e3f-0a1b2c3d4e5f")
LINE = uuid.UUID("3c8d2f1b-2222-4d3e-8f40-1b2c3d4e5f60")


def transcript(sql: Sql) -> None:
    """A ready job, one speaker and one line under the recording, written straight to the database."""
    recording = uuid.UUID(RECORDING)
    sql(
        "INSERT INTO transcription_jobs (id, recording_id, state, language_requested, billed_seconds)"
        " VALUES ($1, $2, 'ready', 'auto', 1)",
        uuid.uuid4(),
        recording,
    )
    sql(
        "INSERT INTO speakers (id, recording_id, label, vendor_label, color_idx) VALUES ($1, $2, 'Speaker 1', 'S0', 0)",
        SPEAKER,
        recording,
    )
    sql(
        "INSERT INTO transcript_lines (id, recording_id, speaker_id, idx, start_ms, end_ms, text, words)"
        " VALUES ($1, $2, $3, 0, 0, 400, 'Hi.', '[]')",
        LINE,
        recording,
        SPEAKER,
    )


def while_trashing(api: Any, auth: dict[str, str], monkeypatch: pytest.MonkeyPatch, write: Callable[[], Any]) -> Any:
    """The recording's delete holds its lock, paused at its last step; `write()` is sent then, and the delete goes on
    once the write waits for a lock (or answered without waiting); the write's answer."""
    real = retention.on_recording_deleted
    entered = threading.Event()
    release = threading.Event()

    async def held(*args: Any) -> None:
        entered.set()
        await asyncio.to_thread(release.wait, 30)
        await real(*args)

    def sql(statement: str) -> list[dict[str, Any]]:
        rows = asyncio.run(admin_on(os.environ["DATABASE_URL"], statement))
        return rows

    monkeypatch.setattr(retention, "on_recording_deleted", held)
    with ThreadPoolExecutor(2) as pool:
        trashed = pool.submit(api.delete, f"/v1/recordings/{RECORDING}", headers=auth)
        assert entered.wait(10)
        written = pool.submit(write)
        eventually(lambda: blocked_on_a_lock(sql) or written.done(), bool, within_s=10)
        release.set()
        assert trashed.result().status_code == 204
        answer = written.result()
    return answer


WORDS = [{"w": "Bye.", "s": 0, "e": 400, "filler": False, "conf": 0.9}]


def test_trx08_a_line_edit_during_the_trash_is_404_and_writes_nothing(
    api: Any, sql: Sql, monkeypatch: pytest.MonkeyPatch
) -> None:
    auth = register(api)
    put_recording(api, auth)
    transcript(sql)
    body = {"text": "Bye.", "words": WORDS, "speakerId": str(SPEAKER), "rev": 1}
    answer = while_trashing(api, auth, monkeypatch, lambda: api.patch(f"/v1/lines/{LINE}", json=body, headers=auth))
    assert failure(answer) == (404, "not_found")
    assert sql("SELECT text, rev FROM transcript_lines") == [{"text": "Hi.", "rev": 1}]


def test_trx08_a_speaker_rename_during_the_trash_is_404_and_writes_nothing(
    api: Any, sql: Sql, monkeypatch: pytest.MonkeyPatch
) -> None:
    auth = register(api)
    put_recording(api, auth)
    transcript(sql)
    answer = while_trashing(
        api, auth, monkeypatch, lambda: api.patch(f"/v1/speakers/{SPEAKER}", json={"name": "Priya"}, headers=auth)
    )
    assert failure(answer) == (404, "not_found")
    assert sql("SELECT name FROM speakers") == [{"name": None}]


def test_trx08_a_new_speaker_during_the_trash_is_404_and_writes_nothing(
    api: Any, sql: Sql, monkeypatch: pytest.MonkeyPatch
) -> None:
    auth = register(api)
    put_recording(api, auth)
    transcript(sql)
    body = {"label": "Guest", "name": "Arjun", "colorIdx": 2}
    path = f"/v1/recordings/{RECORDING}/speakers/{uuid.uuid4()}"
    answer = while_trashing(api, auth, monkeypatch, lambda: api.put(path, json=body, headers=auth))
    assert failure(answer) == (404, "not_found")
    assert sql("SELECT count(*) AS n FROM speakers") == [{"n": 1}]


def test_shr01_a_share_change_during_the_trash_is_404_and_writes_nothing(
    api: Any, sql: Sql, monkeypatch: pytest.MonkeyPatch
) -> None:
    auth = register(api)
    put_recording(api, auth)
    slug = create_share(api, auth).json()["slug"]
    answer = while_trashing(
        api, auth, monkeypatch, lambda: api.patch(f"/v1/shares/{slug}", json={"expiresIn": "7d"}, headers=auth)
    )
    assert failure(answer) == (404, "not_found")
    assert sql("SELECT state, expires_at FROM shares") == [{"state": "disabled", "expires_at": None}]


def test_cld04_a_share_delete_during_the_trash_keeps_the_trash_time(
    api: Any, sql: Sql, monkeypatch: pytest.MonkeyPatch
) -> None:
    auth = register(api)
    put_recording(api, auth)
    slug = create_share(api, auth).json()["slug"]
    answer = while_trashing(api, auth, monkeypatch, lambda: api.delete(f"/v1/shares/{slug}", headers=auth))
    assert answer.status_code == 204
    trashed = sql("SELECT deleted_at FROM recordings")[0]["deleted_at"]
    assert sql("SELECT deleted_at FROM shares") == [{"deleted_at": trashed}]


def test_cld04_a_second_trash_keeps_the_first_trash_time(api: Any, sql: Sql) -> None:
    auth = register(api)
    put_recording(api, auth)
    api.delete(f"/v1/recordings/{RECORDING}", headers=auth)
    first = sql("SELECT deleted_at FROM recordings")
    assert api.delete(f"/v1/recordings/{RECORDING}", headers=auth).status_code == 204
    assert sql("SELECT deleted_at FROM recordings") == first


def test_trx09_roman_words_kept_while_the_recording_is_trashed_never_deadlock(
    api: Any, sql: Sql, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep writes two lines in the reverse of their stored order while the delete marks them in order; keep holds
    the recording's lock first, so the delete waits for it instead of each waiting on the other's line."""
    auth = register(api)
    put_recording(api, auth)
    transcript(sql)
    second = uuid.uuid4()
    sql(
        "INSERT INTO transcript_lines (id, recording_id, speaker_id, idx, start_ms, end_ms, text, words)"
        " VALUES ($1, $2, $3, 1, 400, 800, 'Yes.', '[]')",
        second,
        uuid.UUID(RECORDING),
        SPEAKER,
    )
    real = transliteration.update
    built: list[int] = []
    first_written = threading.Event()
    release = threading.Event()

    def slow_second(*args: Any) -> Any:
        built.append(1)
        if len(built) == 2:
            first_written.set()
            assert release.wait(30)
        statement = real(*args)
        return statement

    monkeypatch.setattr(transliteration, "update", slow_second)

    async def keep() -> None:
        config = load(os.environ).cloud
        assert config is not None
        cloud = await open_cloud(config)
        try:
            async with cloud.sessions() as db:
                lines = await transliteration.recordings.live_lines(db, uuid.UUID(RECORDING))
                await db.commit()
                await transliteration.keep(db, [(line, ("r", [])) for line in reversed(lines)])
        finally:
            await cloud.close()

    with ThreadPoolExecutor(2) as pool:
        kept = pool.submit(asyncio.run, keep())
        assert first_written.wait(10)
        trashed = pool.submit(api.delete, f"/v1/recordings/{RECORDING}", headers=auth)
        waiting = eventually(lambda: waited_tables(sql), bool, within_s=10)
        # The delete waits for the recording's row, which keep took before its first write; never for a line.
        assert waiting == ["recordings"], waiting
        release.set()
        kept.result()
        assert trashed.result().status_code == 204
    assert sql("SELECT roman_text, deleted_at IS NOT NULL AS gone FROM transcript_lines ORDER BY idx") == [
        {"roman_text": "r", "gone": True},
        {"roman_text": "r", "gone": True},
    ]


def test_trx08_another_devices_new_speaker_is_not_found_and_writes_nothing(api: Any, sql: Sql) -> None:
    auth = register(api)
    put_recording(api, auth)
    transcript(sql)
    other = register(api, str(uuid.uuid4()))
    body = {"label": "Guest", "name": "Arjun", "colorIdx": 2}
    answer = api.put(f"/v1/recordings/{RECORDING}/speakers/{uuid.uuid4()}", json=body, headers=other)
    assert failure(answer) == (404, "not_found")
    assert sql("SELECT count(*) AS n FROM speakers") == [{"n": 1}]


def test_cld04_an_audio_complete_during_the_trash_is_404_and_the_upload_stays_open(
    api: Any, sql: Sql, monkeypatch: pytest.MonkeyPatch
) -> None:
    import httpx

    auth = register(api)
    put_recording(api, auth)
    body = {"kind": "audio", "bytes": 3, "contentType": "audio/mp4"}
    started = api.post(f"/v1/recordings/{RECORDING}/uploads", json=body, headers=auth).json()
    etag = httpx.put(started["urls"][0]["url"], content=b"m4a").headers["etag"]
    parts = {"parts": [{"n": 1, "etag": etag}]}
    answer = while_trashing(
        api,
        auth,
        monkeypatch,
        lambda: api.post(f"/v1/uploads/{started['uploadId']}/complete", json=parts, headers=auth),
    )
    assert failure(answer) == (404, "not_found")
    assert sql("SELECT state, completed_at FROM uploads") == [{"state": "uploading", "completed_at": None}]


def waited_tables(sql: Sql) -> list[str]:
    """The tables whose rows this database's waiting transactions wait for (pg_locks): a waiter holds the tuple lock
    of the row it waits on while it waits for the holder's transaction."""
    rows = sql(
        "SELECT DISTINCT c.relname FROM pg_locks w JOIN pg_locks t ON t.pid = w.pid AND t.locktype = 'tuple'"
        " JOIN pg_class c ON c.oid = t.relation JOIN pg_database d ON d.oid = t.database"
        " WHERE NOT w.granted AND d.datname = current_database()"
    )
    return [row["relname"] for row in rows]


def blocked_on_a_lock(sql: Sql) -> bool:
    """Whether a transaction of this database waits for another's (pg_locks)."""
    rows = sql(
        "SELECT count(*) AS n FROM pg_locks w JOIN pg_stat_activity a ON a.pid = w.pid"
        " WHERE NOT w.granted AND a.datname = current_database()"
    )
    return rows[0]["n"] > 0


def test_shr01_a_share_create_queued_behind_a_complete_never_holds_up_the_devices_other_requests(
    api: Any, sql: Sql
) -> None:
    """A complete holds recording A's lock through S3; a share create on A queues behind it. It takes the recording
    first and the device after, so meanwhile the device's other requests (a token check that refreshes
    `last_seen_at`, a share on recording B) don't wait for A."""
    import httpx
    from conftest import VIDEO_SIZE, eventually

    auth = register(api)
    put_recording(api, auth)
    other_recording = str(uuid.uuid4())
    put_recording(api, auth, other_recording)
    share = create_share(api, auth).json()["shareId"]
    upload = {"kind": "share", "bytes": 10, "contentType": "video/mp4", "shareId": share, **VIDEO_SIZE}
    first = api.post(f"/v1/recordings/{RECORDING}/uploads", json=upload, headers=auth).json()
    etag = httpx.put(first["urls"][0]["url"], content=b"ten bytes!").headers["etag"]
    storage = api.app.state.cloud.storage
    real = storage.complete
    inside = threading.Event()
    release = threading.Event()

    async def held(*args: Any) -> None:
        inside.set()
        await asyncio.to_thread(release.wait, 60)
        await real(*args)

    storage.complete = held
    try:
        with ThreadPoolExecutor(4) as pool:
            completing = pool.submit(
                api.post,
                f"/v1/uploads/{first['uploadId']}/complete",
                json={"parts": [{"n": 1, "etag": etag}]},
                headers=auth,
            )
            assert inside.wait(10)
            queued = pool.submit(create_share, api, auth)
            assert eventually(lambda: blocked_on_a_lock(sql), bool, within_s=10)
            sql("UPDATE devices SET last_seen_at = now() - interval '1 hour'")
            checked = pool.submit(api.get, f"/v1/recordings/{RECORDING}/shares", headers=auth)
            elsewhere = pool.submit(create_share, api, auth, other_recording)
            answers = (checked.result(timeout=10), elsewhere.result(timeout=10))
            assert not completing.done()
            release.set()
            assert (completing.result().status_code, queued.result().status_code) == (200, 200)
    finally:
        release.set()
        del storage.complete
    assert [answer.status_code for answer in answers] == [200, 201]


def test_trx08_a_new_speaker_reusing_a_deleted_speakers_id_is_not_found(api: Any, sql: Sql) -> None:
    auth = register(api)
    put_recording(api, auth)
    transcript(sql)
    sql("UPDATE speakers SET deleted_at = now()")
    body = {"label": "Guest", "name": "Arjun", "colorIdx": 2}
    answer = api.put(f"/v1/recordings/{RECORDING}/speakers/{SPEAKER}", json=body, headers=auth)
    assert failure(answer) == (404, "not_found")
    assert sql("SELECT label, name FROM speakers") == [{"label": "Speaker 1", "name": None}]


def test_cld04_trashing_a_recording_keeps_the_delete_time_of_its_share_deleted_earlier(api: Any, sql: Sql) -> None:
    auth = register(api)
    put_recording(api, auth)
    slug = create_share(api, auth).json()["slug"]
    api.delete(f"/v1/shares/{slug}", headers=auth)
    sql("UPDATE shares SET deleted_at = deleted_at - interval '1 day'")
    deleted = sql("SELECT deleted_at FROM shares")
    assert api.delete(f"/v1/recordings/{RECORDING}", headers=auth).status_code == 204
    assert sql("SELECT deleted_at FROM shares") == deleted
