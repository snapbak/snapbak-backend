"""The daily-minutes statements (CLD-03), compiled as Postgres gets them; their behaviour under concurrency is in
tests/integration/test_job_races.py."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import ClauseElement
from sqlalchemy.dialects import postgresql

from snapbak_backend.db.models import TranscriptionJob
from snapbak_backend.services.usage import refund_of, reservation, resets_at

DEVICE = uuid.UUID("0f5b6c1a-3a59-4a63-9d0e-6d5e7f1c9b20")


def sql(statement: ClauseElement) -> str:
    compiled = statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    return " ".join(str(compiled).split())


def test_cld03_the_reservation_adds_to_todays_row_only_while_it_stays_within_7200_seconds() -> None:
    said = sql(reservation(DEVICE, 613))
    assert said.startswith("INSERT INTO usage_daily (device_id, day, audio_seconds) VALUES")
    assert "CAST(timezone('UTC', now()) AS DATE), 613)" in said
    assert said.endswith(
        "ON CONFLICT (device_id, day) DO UPDATE SET audio_seconds = (usage_daily.audio_seconds + "
        "excluded.audio_seconds) WHERE usage_daily.audio_seconds + excluded.audio_seconds <= 7200 "
        "RETURNING usage_daily.audio_seconds"
    )


def test_cld03_a_refund_goes_to_the_utc_day_the_job_was_made_floored_at_0() -> None:
    job = TranscriptionJob(
        recording_id=uuid.UUID("8a1d4c52-0f0e-4d55-9a44-2f1a7e6c3b10"),
        billed_seconds=613,
        created_at=datetime(2026, 10, 17, 23, 59, tzinfo=UTC),
    )
    said = sql(refund_of(job))
    assert said.startswith("UPDATE usage_daily SET audio_seconds=greatest(usage_daily.audio_seconds - 613, 0)")
    assert said.endswith("AND usage_daily.day = '2026-10-17'")
    assert "SELECT recordings.device_id FROM recordings WHERE recordings.id = '8a1d4c52" in said


def test_cld03_the_limit_resets_at_the_next_utc_midnight() -> None:
    assert resets_at(datetime(2026, 10, 17, 4, 32, tzinfo=UTC)) == datetime(2026, 10, 18, tzinfo=UTC)
    assert resets_at(datetime(2026, 10, 17, 23, 59, 59, tzinfo=UTC)) == datetime(2026, 10, 18, tzinfo=UTC)
