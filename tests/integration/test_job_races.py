"""Concurrent Generates against the real Postgres: the unique partial index and the usage reservation hold."""

import asyncio
import os
import uuid
from collections.abc import Coroutine, Iterable
from datetime import UTC, datetime
from typing import Any

from conftest import Sql

from snapbak_backend.config.settings import load
from snapbak_backend.db.models import Recording
from snapbak_backend.services import jobs
from snapbak_backend.services.cloud import open_cloud
from snapbak_backend.services.failures import CloudFailure


def recording_rows(sql: Sql, count: int, duration_ms: int) -> tuple[uuid.UUID, list[uuid.UUID]]:
    device = uuid.uuid4()
    sql("INSERT INTO devices (id, install_id, token_hash) VALUES ($1, $1, $2)", device, os.urandom(32))
    made = [uuid.uuid4() for _ in range(count)]
    for recording in made:
        sql(
            "INSERT INTO recordings (id, device_id, title, duration_ms, created_at) VALUES ($1, $2, 't', $3, now())",
            recording,
            device,
            duration_ms,
        )
    return device, made


async def together(calls: Iterable[Coroutine[Any, Any, int]]) -> list[int]:
    answers = await asyncio.gather(*calls)
    return list(answers)


async def generate(device: uuid.UUID, recording_id: uuid.UUID, duration_ms: int) -> int:
    """One Generate in its own session and connection: 201 or the error's status."""
    config = load(os.environ).cloud
    assert config is not None
    cloud = await open_cloud(config)
    try:
        async with cloud.sessions() as db:
            recording = Recording(
                id=recording_id, device_id=device, title="t", duration_ms=duration_ms, created_at=datetime.now(UTC)
            )
            try:
                await jobs.create(db, recording, "auto", "native", "fake:two-speakers-en")
            except CloudFailure as failure:
                return failure.status
            return 201
    finally:
        await cloud.close()


def test_trx01_two_concurrent_generates_make_one_job_and_one_409_that_reserves_nothing(sql: Sql) -> None:
    device, (recording,) = recording_rows(sql, 1, 60_000)
    answers = asyncio.run(together(generate(device, recording, 60_000) for _ in range(2)))
    assert sorted(answers) == [201, 409]
    assert sql("SELECT count(*) AS n FROM transcription_jobs") == [{"n": 1}]
    assert sql("SELECT audio_seconds FROM usage_daily") == [{"audio_seconds": 60}]


def test_cld03_ten_concurrent_15_minute_generates_give_8_jobs_and_2_daily_limits(sql: Sql) -> None:
    device, made = recording_rows(sql, 10, 900_000)
    answers = asyncio.run(together(generate(device, recording, 900_000) for recording in made))
    assert sorted(answers) == [201] * 8 + [429] * 2
    assert sql("SELECT audio_seconds FROM usage_daily") == [{"audio_seconds": 7200}]
