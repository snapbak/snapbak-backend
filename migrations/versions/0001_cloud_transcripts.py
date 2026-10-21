"""The cloud transcript's tables (13-transcription-pipeline LLD, Postgres schema).

Revision ID: 0001_cloud_transcripts
Revises:
"""

from alembic import op

revision = "0001_cloud_transcripts"
down_revision = None

UPGRADE = """
CREATE TABLE devices (
    id                  uuid PRIMARY KEY,
    install_id          uuid NOT NULL UNIQUE,
    token_hash          bytea NOT NULL UNIQUE,
    created_at          timestamptz NOT NULL DEFAULT now(),
    last_seen_at        timestamptz NOT NULL DEFAULT now(),
    voiceprint_consent  boolean,
    consent_at          timestamptz,
    deleted_at          timestamptz
);

CREATE TABLE recordings (
    id           uuid PRIMARY KEY,
    device_id    uuid NOT NULL REFERENCES devices (id),
    title        text NOT NULL CHECK (length(title) <= 300),
    duration_ms  integer NOT NULL CHECK (duration_ms >= 0),
    created_at   timestamptz NOT NULL,
    updated_at   timestamptz NOT NULL DEFAULT now(),
    deleted_at   timestamptz
);
CREATE INDEX recordings_device ON recordings (device_id) WHERE deleted_at IS NULL;

CREATE TABLE uploads (
    id            uuid PRIMARY KEY,
    recording_id  uuid NOT NULL REFERENCES recordings (id),
    share_id      uuid,
    kind          text NOT NULL CHECK (kind IN ('audio', 'share')),
    s3_key        text NOT NULL,
    upload_id     text NOT NULL,
    part_size     integer NOT NULL,
    bytes         bigint NOT NULL CHECK (bytes > 0),
    content_type  text NOT NULL,
    parts         jsonb,
    state         text NOT NULL DEFAULT 'uploading' CHECK (state IN ('uploading', 'complete', 'aborted')),
    created_at    timestamptz NOT NULL DEFAULT now(),
    completed_at  timestamptz,
    deleted_at    timestamptz
);
CREATE INDEX uploads_recording ON uploads (recording_id, kind, state);

CREATE TABLE transcription_jobs (
    id                  uuid PRIMARY KEY,
    recording_id        uuid NOT NULL REFERENCES recordings (id),
    upload_id           uuid REFERENCES uploads (id),
    state               text NOT NULL DEFAULT 'queued' CHECK (state IN ('queued', 'processing', 'ready', 'failed')),
    stage               text CHECK (stage IN ('transcribing', 'diarizing', 'merging')),
    failure_reason      text,
    language_requested  text NOT NULL,
    language_detected   text,
    script              text NOT NULL DEFAULT 'native' CHECK (script IN ('native', 'roman')),
    strategy            text NOT NULL DEFAULT 'assemblyai+pyannote',
    vendor_refs         jsonb NOT NULL DEFAULT '{}',
    attempt             integer NOT NULL DEFAULT 1,
    billed_seconds      integer NOT NULL,
    version             integer NOT NULL DEFAULT 1,
    submitted_at        timestamptz,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    deleted_at          timestamptz
);
CREATE UNIQUE INDEX transcription_jobs_one_per_recording
    ON transcription_jobs (recording_id)
    WHERE state IN ('queued', 'processing', 'ready');
CREATE INDEX transcription_jobs_waiting
    ON transcription_jobs (created_at)
    WHERE state = 'queued' AND upload_id IS NULL;

CREATE TABLE speakers (
    id                 uuid PRIMARY KEY,
    recording_id       uuid NOT NULL REFERENCES recordings (id),
    label              text NOT NULL,
    vendor_label       text NOT NULL,
    name               text CHECK (length(name) <= 100),
    name_source        text CHECK (name_source IN ('vendor', 'user')),
    color_idx          smallint NOT NULL CHECK (color_idx BETWEEN 0 AND 7),
    voiceprint         bytea,
    voiceprint_key_id  smallint,
    created_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now(),
    deleted_at         timestamptz,
    UNIQUE (recording_id, label)
);

CREATE TABLE transcript_lines (
    id            uuid PRIMARY KEY,
    recording_id  uuid NOT NULL REFERENCES recordings (id),
    speaker_id    uuid NOT NULL REFERENCES speakers (id),
    idx           integer NOT NULL,
    start_ms      integer NOT NULL,
    end_ms        integer NOT NULL CHECK (end_ms >= start_ms),
    text          text NOT NULL,
    words         jsonb NOT NULL,
    roman_text    text,
    roman_words   jsonb,
    updated_at    timestamptz NOT NULL DEFAULT now(),
    rev           integer NOT NULL DEFAULT 1,
    deleted_at    timestamptz,
    UNIQUE (recording_id, idx)
);

CREATE TABLE caption_tracks (
    id            uuid PRIMARY KEY,
    recording_id  uuid NOT NULL REFERENCES recordings (id),
    language      text NOT NULL,
    lines         jsonb NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    deleted_at    timestamptz
);
CREATE UNIQUE INDEX caption_tracks_one_per_language
    ON caption_tracks (recording_id, language) WHERE deleted_at IS NULL;

CREATE TABLE usage_daily (
    device_id      uuid NOT NULL REFERENCES devices (id),
    day            date NOT NULL,
    audio_seconds  integer NOT NULL DEFAULT 0 CHECK (audio_seconds >= 0),
    deleted_at     timestamptz,
    PRIMARY KEY (device_id, day)
);
"""

TABLES = (
    "usage_daily",
    "caption_tracks",
    "transcript_lines",
    "speakers",
    "transcription_jobs",
    "uploads",
    "recordings",
    "devices",
)


def upgrade() -> None:
    # asyncpg runs one statement per call.
    for statement in UPGRADE.split(";")[:-1]:
        op.execute(statement)


def downgrade() -> None:
    for table in TABLES:
        op.execute(f"DROP TABLE {table}")
