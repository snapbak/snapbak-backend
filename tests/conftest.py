import asyncio
import base64
import contextlib
import functools
import json
import os
import secrets
import socket
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import asyncpg
import boto3
import httpx
import pytest
import redis
from alembic import command
from alembic.config import Config as AlembicConfig

ROOT = Path(__file__).parents[1]
# The compose stack (docker-compose.yml, `just db-up`). Tests that need it skip, with this reason, when it is down.
POSTGRES = os.environ.get("SNAPBAK_TEST_POSTGRES", "postgresql://snapbak:dev@127.0.0.1:15432/snapbak")
REDIS_SERVER = "redis://127.0.0.1:16379"
CLAIM = "snapbak:test-run"


def claim_redis_db() -> str:
    """Each run claims a free Redis database (2 to 15; 0 is `just serve`'s) with a key only it sets, as it takes its
    own Postgres database and bucket, so runs at the same time never flush or take each other's jobs. The claim
    lapses after an hour if a run dies; `pytest_sessionfinish` gives it back."""
    for db in range(2, 16):
        try:
            if redis.Redis.from_url(f"{REDIS_SERVER}/{db}").set(CLAIM, os.getpid(), nx=True, ex=3600):
                return f"{REDIS_SERVER}/{db}"
        except redis.ConnectionError:
            break
    return f"{REDIS_SERVER}/2"


REDIS = os.environ.get("SNAPBAK_TEST_REDIS") or claim_redis_db()


def pytest_sessionfinish() -> None:
    client = redis.Redis.from_url(REDIS)
    with contextlib.suppress(redis.ConnectionError):
        if client.get(CLAIM) == str(os.getpid()).encode():
            client.delete(CLAIM)


DOWN = "the compose stack is not running: start it with `just db-up` (docs/cloud.md)"
# Test buckets live on real S3, under the `snapbak` IAM user's key in `.env` (which `just test` loads); never the Mac's
# own AWS profile or `AWS_*` variables, which may be another account.
NO_AWS = "no Snapbak AWS key for the test buckets: set SNAPBAK_AWS_ACCESS_KEY_ID in .env (SETUP.md)"
REGION = "ap-south-1"
TABLES = (
    "meeting_chunks, meeting_pins, meetings, shares, usage_daily, caption_tracks, transcript_lines, speakers, "
    "transcription_jobs, uploads, recordings, devices, early_access_spots, waitlist_entries, contact_messages"
)

type Sql = Callable[..., list[dict[str, Any]]]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def dictation_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """A `.env` with DATABASE_URL must not turn the dictation tests into cloud ones; `cloud` sets it again."""
    monkeypatch.delenv("DATABASE_URL", raising=False)


def reachable(url: str) -> bool:
    rest = url.split("@")[-1].split("//")[-1]
    host, _, port = rest.split("/")[0].partition(":")
    try:
        with socket.create_connection((host, int(port)), timeout=0.3):
            return True
    except OSError:
        return False


async def admin(statement: str) -> None:
    connection = await asyncpg.connect(POSTGRES)
    try:
        await connection.execute(statement)
    finally:
        await connection.close()


def alembic(url: str) -> AlembicConfig:
    config = AlembicConfig(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", url)
    return config


@functools.cache
def s3() -> Any:
    """One client for the run: each call reaches ap-south-1, so its connections are worth keeping."""
    return boto3.client(
        "s3",
        region_name=REGION,
        aws_access_key_id=os.environ["SNAPBAK_AWS_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["SNAPBAK_AWS_SECRET_ACCESS_KEY"],
    )


def empty_bucket(bucket: str) -> None:
    client = s3()
    for upload in client.list_multipart_uploads(Bucket=bucket).get("Uploads", []):
        client.abort_multipart_upload(Bucket=bucket, Key=upload["Key"], UploadId=upload["UploadId"])
    keys = [{"Key": item["Key"]} for item in client.list_objects_v2(Bucket=bucket).get("Contents", [])]
    if keys:
        client.delete_objects(Bucket=bucket, Delete={"Objects": keys})


@pytest.fixture(scope="session")
def database() -> Iterator[str]:
    """A fresh database for this test run, migrated to head; its asyncpg URL."""
    if not reachable(POSTGRES):
        absent(DOWN)
    name = f"snapbak_test_{os.getpid()}"
    asyncio.run(admin(f"DROP DATABASE IF EXISTS {name}"))
    asyncio.run(admin(f"CREATE DATABASE {name}"))
    url = POSTGRES.rsplit("/", 1)[0].replace("postgresql://", "postgresql+asyncpg://") + f"/{name}"
    command.upgrade(alembic(url), "head")
    yield url
    asyncio.run(admin(f"DROP DATABASE {name} WITH (FORCE)"))


def absent(reason: str) -> None:
    """A service the cloud tests need is missing: a skip for a plain `pytest`, a failure under `just test`
    (SNAPBAK_REQUIRE_CLOUD), so a run that covered none of them never reads as green."""
    if os.environ.get("SNAPBAK_REQUIRE_CLOUD"):
        pytest.fail(reason)
    pytest.skip(reason)


@pytest.fixture(scope="session")
def bucket() -> Iterator[str]:
    """The run's own bucket, never the shared `snapbak-media` one. Bucket names are global across AWS, hence the random
    part; the IAM policy allows `snapbak-test-*` (SETUP.md)."""
    if not os.environ.get("SNAPBAK_AWS_ACCESS_KEY_ID"):
        absent(NO_AWS)
    name = f"snapbak-test-{secrets.token_hex(4)}"
    s3().create_bucket(Bucket=name, CreateBucketConfiguration={"LocationConstraint": REGION})
    yield name
    empty_bucket(name)
    s3().delete_bucket(Bucket=name)


@pytest.fixture
def cloud(database: str, bucket: str, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """The environment of a server and worker on the compose stack and the run's S3 bucket, with every table, the
    Redis DB and the bucket emptied."""
    if not reachable(REDIS):
        absent(DOWN)
    asyncio.run(admin_on(database, f"TRUNCATE {TABLES}"))
    client = redis.Redis.from_url(REDIS)
    client.flushdb()
    client.set(CLAIM, os.getpid(), ex=3600)
    empty_bucket(bucket)
    env = {
        "SNAPBAK_TOKEN": "dev",
        "SNAPBAK_POLISH": "fake:edit",
        "SNAPBAK_BIG": "fake:edit",
        "SNAPBAK_FALLBACK": "",
        "DATABASE_URL": database,
        "REDIS_URL": REDIS,
        "S3_REGION": REGION,
        "S3_BUCKET": bucket,
        "SNAPBAK_PUBLIC_URL": "http://testserver",
        "SNAPBAK_SHARE_BASE_URL": "http://testserver",
        "SNAPBAK_HOME_URL": "https://snapbak.app",
        "SNAPBAK_TRANSCRIPTION": "fake:two-speakers-en",
        "SNAPBAK_WEBHOOK_SECRET": "hook-secret",
        "PYANNOTE_WEBHOOK_SECRET": "pyannote-secret",
        "VOICEPRINT_KEYS": json.dumps({"1": base64.b64encode(bytes(32)).decode()}),
        "SNAPBAK_WAITLIST_TOKEN": "waitlist-token",
    }
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    return env


async def admin_on(url: str, statement: str, *args: Any) -> list[dict[str, Any]]:
    connection = await asyncpg.connect(url.replace("+asyncpg", ""))
    try:
        rows = await connection.fetch(statement, *args)
    finally:
        await connection.close()
    return [dict(row) for row in rows]


@pytest.fixture
def sql(cloud: dict[str, str]) -> Sql:
    """Runs one statement on the test database and returns its rows, for assertions on stored state."""

    def run(statement: str, *args: Any) -> list[dict[str, Any]]:
        return asyncio.run(admin_on(cloud["DATABASE_URL"], statement, *args))

    return run


@pytest.fixture
def api(cloud: dict[str, str]) -> Iterator[Any]:
    """The app on the compose stack, started and stopped like the real server."""
    from fastapi.testclient import TestClient

    from snapbak_backend import main

    # The app starts once per process in production; each test here starts it again, so its routes are put back.
    routes = list(main.app.router.routes)
    with TestClient(main.app, raise_server_exceptions=False) as client:
        yield client
    main.app.router.routes[:] = routes


INSTALL = "6b0d0c7e-6b2f-4b1e-9f53-2f6f4e0f2a11"


def register(api: Any, install: str = INSTALL) -> dict[str, str]:
    """A registered device's auth header."""
    answer = api.post("/v1/devices", json={"installId": install, "appVersion": "1.0.0"}).json()
    return {"Authorization": f"Bearer {answer['token']}"}


RECORDING = "8a1d4c52-0f0e-4d55-9a44-2f1a7e6c3b10"


def put_recording(
    api: Any,
    auth: dict[str, str],
    recording: str = RECORDING,
    duration_ms: int = 612_400,
    recording_type: str | None = None,
) -> Any:
    body = {"title": "Recording 2026-10-17 at 10.02.11", "durationMs": duration_ms, "createdAt": "2026-10-17T04:32:11Z"}
    if recording_type is not None:
        body["recordingType"] = recording_type
    return api.put(f"/v1/recordings/{recording}", json=body, headers=auth)


MIB = 1024 * 1024


def upload_audio(api: Any, auth: dict[str, str], data: bytes, recording: str = RECORDING) -> tuple[str, Any]:
    """Starts an audio upload of `data`, PUTs every part to its presigned URL, completes it; the upload ID and the
    complete answer."""
    body = {"kind": "audio", "bytes": len(data), "contentType": "audio/mp4"}
    answer = upload(api, auth, data, body, recording)
    return answer


# The tests' own requests to S3's presigned URLs, on one pooled client. S3 now and then resets a new TLS
# connection ("Connection reset by peer"); the transport asks again only when the connect failed, before a byte of
# the request was sent, so no answer a test checks is ever retried.
S3_HTTP = httpx.Client(transport=httpx.HTTPTransport(retries=3), timeout=30)


def upload(api: Any, auth: dict[str, str], data: bytes, body: dict[str, Any], recording: str) -> tuple[str, Any]:
    started = api.post(f"/v1/recordings/{recording}/uploads", json=body, headers=auth).json()
    size = started["partSize"]
    parts = []
    for item in started["urls"]:
        n = item["n"]
        put = S3_HTTP.put(item["url"], content=data[(n - 1) * size : n * size])
        parts.append({"n": n, "etag": put.headers["etag"]})
    done = api.post(f"/v1/uploads/{started['uploadId']}/complete", json={"parts": parts}, headers=auth)
    return started["uploadId"], done


SHARE = {
    "title": "Recording 2026-10-17 at 10.02.11",
    "bytes": 39_845_888,
    "expiresIn": "never",
}
# The encoded video's length and size, sent with its upload.
VIDEO_SIZE = {"durationMs": 61_200, "width": 1920, "height": 1080}


def create_share(api: Any, auth: dict[str, str], recording: str = RECORDING, **changes: Any) -> Any:
    return api.post(f"/v1/recordings/{recording}/shares", json={**SHARE, **changes}, headers=auth)


def upload_share(
    api: Any, auth: dict[str, str], share_id: str, data: bytes, recording: str = RECORDING, **size: int
) -> tuple[str, Any]:
    """The share's video as the app sends it: every part PUT, then complete; the upload ID and the complete answer."""
    body = {"kind": "share", "bytes": len(data), "contentType": "video/mp4", "shareId": share_id, **VIDEO_SIZE, **size}
    answer = upload(api, auth, data, body, recording)
    return answer


def share_live(api: Any, auth: dict[str, str], video: bytes = b"mp4 video", poster: bytes | None = b"jpeg") -> Any:
    """A recording shared end to end, as the app does it: the recording, the share, the poster, the video; the
    create answer."""
    put_recording(api, auth)
    made = create_share(api, auth).json()
    if poster is not None:
        S3_HTTP.put(made["posterUploadUrl"], content=poster, headers={"Content-Type": "image/jpeg"})
    upload_share(api, auth, made["shareId"], video)
    return made


def create_job(api: Any, auth: dict[str, str], language: str = "auto", recording: str = RECORDING) -> Any:
    return api.post(
        f"/v1/recordings/{recording}/transcription", json={"language": language, "script": "native"}, headers=auth
    )


def queued_arq_jobs() -> list[str]:
    """The arq job IDs waiting in the test Redis."""
    keys = redis.Redis.from_url(REDIS).scan_iter("arq:job:*")
    return sorted(key.decode().removeprefix("arq:job:") for key in keys)


async def fake_cut(audio: Path, start_ms: int, length_ms: int, out: Path) -> None:
    """Stands in for ffmpeg, which the test machine may lack; the real cut has its own test."""
    out.write_bytes(b"RIFF fake clip")


@pytest.fixture
def start_worker(cloud: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[[str], None]]:
    """Starts the arq worker in a thread of its own, on a fake strategy fixture, as `just worker` would."""
    import threading

    from arq.connections import RedisSettings
    from arq.worker import Worker

    from snapbak_backend import worker as tasks

    running: dict[str, Any] = {}

    def start(fixture: str = "two-speakers-en") -> None:
        monkeypatch.setenv("SNAPBAK_TRANSCRIPTION", f"fake:{fixture}")
        ready = threading.Event()

        async def main() -> None:
            made = Worker(
                functions=tasks.FUNCTIONS,
                redis_settings=RedisSettings.from_dsn(REDIS),
                on_startup=tasks.startup,
                on_shutdown=tasks.shutdown,
                poll_delay=0.02,
                max_tries=tasks.MAX_TRIES,
                handle_signals=False,
                ctx={"cut": fake_cut},
            )
            running["loop"] = asyncio.get_running_loop()
            running["task"] = asyncio.current_task()
            ready.set()
            try:
                await made.async_run()
            finally:
                await made.close()

        def run() -> None:
            with contextlib.suppress(asyncio.CancelledError):
                asyncio.run(main())

        running["thread"] = threading.Thread(target=run, daemon=True)
        running["thread"].start()
        ready.wait(5)

    yield start
    if "loop" in running:
        # The cancel below must not land on a task mid-transaction: its connection would stay checked out with the
        # transaction open, and the next test's TRUNCATE would wait on it forever. Wait for the queue and the
        # in-flight jobs to empty first (a Retry defers by seconds at most).
        client = redis.Redis.from_url(REDIS)
        eventually(
            lambda: (client.zcard("arq:queue"), client.hlen("arq:in_progress")),
            lambda counts: counts == (0, 0),
            within_s=10,
        )
        running["loop"].call_soon_threadsafe(running["task"].cancel)
        running["thread"].join(10)
        # And whatever the cancel still caught mid-rollback: the worker's engine dies with its loop, so its
        # connections are closed here, server-side, or the next test's TRUNCATE would wait on them forever.
        url = os.environ.get("DATABASE_URL", "")
        if url:
            name = url.rsplit("/", 1)[-1]
            with contextlib.suppress(Exception):
                asyncio.run(
                    admin(
                        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity"
                        f" WHERE datname = '{name}' AND pid <> pg_backend_pid()"
                    )
                )


def follow(api: Any, auth: dict[str, str], job: str) -> dict[str, Any]:
    """Long-polls the job as the app does until it is ready or failed; every answer, in order."""
    since = 0
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        answer = api.get(f"/v1/jobs/{job}?wait=5&since={since}", headers=auth).json()
        since = answer["version"]
        if answer["state"] in ("ready", "failed"):
            return answer
    raise AssertionError(f"job {job} didn't finish in 15 s")


def eventually[T](read: Callable[[], T], done: Callable[[T], bool], within_s: float = 5) -> T:
    """`read()` once `done` holds for it, polled every 50 ms; the last read when time runs out."""
    deadline = time.monotonic() + within_s
    value = read()
    while not done(value) and time.monotonic() < deadline:
        time.sleep(0.05)
        value = read()
    return value


def generate(api: Any, auth: dict[str, str], language: str = "auto", script: str = "native") -> str:
    """Register-free Generate: the recording, the job, the audio upload and its complete; the job ID."""
    put_recording(api, auth)
    body = {"language": language, "script": script}
    job = api.post(f"/v1/recordings/{RECORDING}/transcription", json=body, headers=auth).json()["jobId"]
    upload_audio(api, auth, b"fake m4a bytes")
    return job


def failure(response: Any) -> tuple[int, str]:
    """A cloud error answer as `(status, code)`."""
    code = response.json()["error"]["code"]
    return response.status_code, code


def ran(job_id: str) -> bool:
    """Whether the worker finished the arq job (its result is kept an hour)."""
    return bool(redis.Redis.from_url(REDIS).exists(f"arq:result:{job_id}"))


def await_tasks(*job_ids: str) -> None:
    """Waits until the worker finished every named arq job, so a test asserts on settled state, never on a sleep."""
    done = eventually(lambda: [ran(job_id) for job_id in job_ids], all)
    assert all(done), dict(zip(job_ids, done, strict=True))


@contextlib.contextmanager
def watching(job: str) -> Iterator[Any]:
    """Subscribed to the job's change channel (`job:{id}`, services/events.py) before the caller acts, so no change
    the worker publishes is missed."""
    pubsub = redis.Redis.from_url(REDIS).pubsub()
    pubsub.subscribe(f"job:{job}")
    pubsub.get_message(timeout=1)
    try:
        yield pubsub
    finally:
        pubsub.close()


def on_change[T](pubsub: Any, read: Callable[[], T], done: Callable[[T], bool], hang_s: float = 120) -> T:
    """`read()` again after each change published on the job's channel, until `done` holds: the wait follows the
    worker, however slow the machine. `hang_s` only stops a run that hung."""
    deadline = time.monotonic() + hang_s
    value = read()
    while not done(value):
        assert time.monotonic() < deadline, f"no change in {hang_s} s: {value}"
        pubsub.get_message(timeout=1)
        value = read()
    return value
