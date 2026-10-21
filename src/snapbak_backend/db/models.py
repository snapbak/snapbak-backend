"""The cloud transcript's tables (docs/cloud.md). Every table has `deleted_at`: rows are soft-deleted, never removed.
Times are integer milliseconds of source time. The CHECK constraints live in the migrations."""

from datetime import date, datetime
from typing import Any, ClassVar, Literal
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    LargeBinary,
    SmallInteger,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

type JobState = Literal["queued", "processing", "ready", "failed"]
type Stage = Literal["transcribing", "diarizing", "merging"]
type Script = Literal["native", "roman"]
type Failure = Literal[
    "unsupported_language",
    "too_long",
    "no_speech",
    "upload_expired",
    "upload_invalid",
    "vendor_error",
    "vendor_timeout",
    "internal",
]
type UploadState = Literal["uploading", "complete", "aborted"]
type ShareState = Literal["uploading", "live", "disabled"]
# What a share's page shows: `expired` and `gone` are computed, never stored (12 HLD, Share states).
type PageState = Literal["live", "uploading", "disabled", "expired", "gone"]
# A job in one of these blocks a second job for its recording (D8, Q20); a failed one doesn't, so Retry works.
LIVE_STATES = ("queued", "processing", "ready")


class Base(DeclarativeBase):
    type_annotation_map: ClassVar = {
        str: Text,
        datetime: DateTime(timezone=True),
        dict[str, Any]: JSONB,
        list[dict[str, Any]]: JSONB,
    }


class WaitlistEntry(Base):
    __tablename__ = "waitlist_entries"
    id: Mapped[UUID] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(unique=True, index=True)
    use_case: Mapped[str | None]
    source: Mapped[str] = mapped_column(server_default="website")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class ContactMessage(Base):
    __tablename__ = "contact_messages"
    id: Mapped[UUID] = mapped_column(primary_key=True)
    name: Mapped[str]
    email: Mapped[str]
    message: Mapped[str]
    source: Mapped[str] = mapped_column(server_default="website")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class EarlyAccessSpot(Base):
    __tablename__ = "early_access_spots"
    id: Mapped[int] = mapped_column(primary_key=True)
    total_spots: Mapped[int]
    claimed_spots: Mapped[int] = mapped_column(server_default="0")
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class Device(Base):
    __tablename__ = "devices"
    id: Mapped[UUID] = mapped_column(primary_key=True)
    install_id: Mapped[UUID] = mapped_column(unique=True)
    token_hash: Mapped[bytes] = mapped_column(LargeBinary, unique=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(server_default=func.now())
    voiceprint_consent: Mapped[bool | None]
    consent_at: Mapped[datetime | None]
    deleted_at: Mapped[datetime | None]


class Recording(Base):
    __tablename__ = "recordings"
    id: Mapped[UUID] = mapped_column(primary_key=True)
    device_id: Mapped[UUID] = mapped_column(ForeignKey("devices.id"))
    title: Mapped[str]
    duration_ms: Mapped[int]
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())
    deleted_at: Mapped[datetime | None]
    __table_args__ = (Index("recordings_device", "device_id", postgresql_where=text("deleted_at IS NULL")),)


class Upload(Base):
    __tablename__ = "uploads"
    id: Mapped[UUID] = mapped_column(primary_key=True)
    recording_id: Mapped[UUID] = mapped_column(ForeignKey("recordings.id"))
    share_id: Mapped[UUID | None] = mapped_column(ForeignKey("shares.id"))
    kind: Mapped[str]
    s3_key: Mapped[str]
    upload_id: Mapped[str]
    part_size: Mapped[int]
    bytes: Mapped[int] = mapped_column(BigInteger)
    content_type: Mapped[str]
    # A share video's length and size, which its share takes when it completes (12 LLD).
    duration_ms: Mapped[int | None]
    width: Mapped[int | None]
    height: Mapped[int | None]
    parts: Mapped[list[dict[str, Any]] | None]
    state: Mapped[UploadState] = mapped_column(Text, server_default="uploading")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    completed_at: Mapped[datetime | None]
    deleted_at: Mapped[datetime | None]
    __table_args__ = (Index("uploads_recording", "recording_id", "kind", "state"),)


class TranscriptionJob(Base):
    __tablename__ = "transcription_jobs"
    id: Mapped[UUID] = mapped_column(primary_key=True)
    recording_id: Mapped[UUID] = mapped_column(ForeignKey("recordings.id"))
    upload_id: Mapped[UUID | None] = mapped_column(ForeignKey("uploads.id"))
    state: Mapped[JobState] = mapped_column(Text, server_default="queued")
    stage: Mapped[Stage | None] = mapped_column(Text)
    failure_reason: Mapped[Failure | None] = mapped_column(Text)
    language_requested: Mapped[str]
    language_detected: Mapped[str | None]
    script: Mapped[Script] = mapped_column(Text, server_default="native")
    strategy: Mapped[str] = mapped_column(server_default="assemblyai+pyannote")
    vendor_refs: Mapped[dict[str, Any]] = mapped_column(server_default="{}")
    attempt: Mapped[int] = mapped_column(server_default="1")
    billed_seconds: Mapped[int]
    version: Mapped[int] = mapped_column(server_default="1")
    submitted_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())
    deleted_at: Mapped[datetime | None]
    __table_args__ = (
        Index(
            "transcription_jobs_one_per_recording",
            "recording_id",
            unique=True,
            postgresql_where=text("state IN ('queued', 'processing', 'ready')"),
        ),
        Index(
            "transcription_jobs_waiting",
            "created_at",
            postgresql_where=text("state = 'queued' AND upload_id IS NULL"),
        ),
    )


class Speaker(Base):
    __tablename__ = "speakers"
    id: Mapped[UUID] = mapped_column(primary_key=True)
    recording_id: Mapped[UUID] = mapped_column(ForeignKey("recordings.id"))
    label: Mapped[str]
    vendor_label: Mapped[str]
    name: Mapped[str | None]
    name_source: Mapped[Literal["vendor", "user"] | None] = mapped_column(Text)
    color_idx: Mapped[int] = mapped_column(SmallInteger)
    voiceprint: Mapped[bytes | None] = mapped_column(LargeBinary)
    voiceprint_key_id: Mapped[int | None] = mapped_column(SmallInteger)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())
    deleted_at: Mapped[datetime | None]
    __table_args__ = (UniqueConstraint("recording_id", "label"),)


class TranscriptLine(Base):
    __tablename__ = "transcript_lines"
    id: Mapped[UUID] = mapped_column(primary_key=True)
    recording_id: Mapped[UUID] = mapped_column(ForeignKey("recordings.id"))
    speaker_id: Mapped[UUID] = mapped_column(ForeignKey("speakers.id"))
    idx: Mapped[int]
    start_ms: Mapped[int]
    end_ms: Mapped[int]
    text: Mapped[str]
    words: Mapped[list[dict[str, Any]]]
    roman_text: Mapped[str | None]
    roman_words: Mapped[list[dict[str, Any]] | None]
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())
    rev: Mapped[int] = mapped_column(server_default="1")
    deleted_at: Mapped[datetime | None]
    __table_args__ = (UniqueConstraint("recording_id", "idx"),)


class CaptionTrack(Base):
    """One language's captions of a recording (TRX-07): `lines` is a list of TrackLine bodies in idx order
    (schemas/translations.py), plus `editRev`, the last edit's counter, never sent."""

    __tablename__ = "caption_tracks"
    id: Mapped[UUID] = mapped_column(primary_key=True)
    recording_id: Mapped[UUID] = mapped_column(ForeignKey("recordings.id"))
    language: Mapped[str]
    # The transcript's detected language when the track was made.
    source_language: Mapped[str] = mapped_column(server_default="")
    lines: Mapped[list[dict[str, Any]]]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())
    deleted_at: Mapped[datetime | None]
    __table_args__ = (
        Index(
            "caption_tracks_one_per_language",
            "recording_id",
            "language",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )


class UsageDaily(Base):
    __tablename__ = "usage_daily"
    device_id: Mapped[UUID] = mapped_column(ForeignKey("devices.id"), primary_key=True)
    day: Mapped[date] = mapped_column(primary_key=True)
    audio_seconds: Mapped[int] = mapped_column(server_default="0")
    translated_lines: Mapped[int] = mapped_column(server_default="0")
    deleted_at: Mapped[datetime | None]


class Share(Base):
    """A recording's link `snapbak.site/v/{slug}` (SHR-01, 12 LLD). One that isn't deleted per recording, so a
    re-export keeps the slug."""

    __tablename__ = "shares"
    id: Mapped[UUID] = mapped_column(primary_key=True)
    recording_id: Mapped[UUID] = mapped_column(ForeignKey("recordings.id"))
    slug: Mapped[str] = mapped_column(unique=True)
    s3_key: Mapped[str | None]
    poster_s3_key: Mapped[str | None]
    state: Mapped[ShareState] = mapped_column(Text, server_default="uploading")
    expires_at: Mapped[datetime | None]
    title: Mapped[str]
    version: Mapped[int] = mapped_column(server_default="0")
    video_bytes: Mapped[int | None] = mapped_column(BigInteger)
    duration_ms: Mapped[int | None]
    width: Mapped[int | None]
    height: Mapped[int | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())
    deleted_at: Mapped[datetime | None]
    __table_args__ = (
        Index("shares_one_per_recording", "recording_id", unique=True, postgresql_where=text("deleted_at IS NULL")),
    )


def page_state(share: Share, now: datetime) -> PageState:
    if share.deleted_at is not None:
        return "gone"
    expires = share.expires_at
    if share.state == "live" and expires is not None and expires <= now:
        return "expired"
    return share.state
