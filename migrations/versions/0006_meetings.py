"""The meetings tables, the recording and share types, and the meeting LLM caps (MEETINGS.md, D11/D20/D33/D38).

Revision ID: 0006_meetings
Revises: 0005_marketing_site
"""

from alembic import op

revision = "0006_meetings"
down_revision = "0005_marketing_site"

UPGRADE = """
CREATE EXTENSION IF NOT EXISTS vector;

ALTER TABLE recordings ADD COLUMN recording_type text NOT NULL DEFAULT 'video'
    CHECK (recording_type IN ('video', 'audio'));

CREATE TABLE meetings (
    id                         uuid PRIMARY KEY,
    recording_id               uuid NOT NULL REFERENCES recordings (id),
    title                      text NOT NULL CHECK (length(title) <= 300),
    state                      text NOT NULL DEFAULT 'awaiting_transcript'
        CHECK (state IN ('awaiting_transcript', 'generating', 'ready', 'failed')),
    minutes                    jsonb,
    minutes_failure            text,
    minutes_model              text,
    minutes_generated_at       timestamptz,
    transcript_version         integer NOT NULL DEFAULT 0,
    minutes_transcript_version integer,
    minutes_run                uuid,
    minutes_clicked            boolean NOT NULL DEFAULT false,
    minutes_attempts           integer NOT NULL DEFAULT 0,
    title_set_by_user          boolean NOT NULL DEFAULT false,
    version                    integer NOT NULL DEFAULT 1,
    started_at                 timestamptz,
    ended_at                   timestamptz,
    created_at                 timestamptz NOT NULL DEFAULT now(),
    updated_at                 timestamptz NOT NULL DEFAULT now(),
    deleted_at                 timestamptz
);
CREATE UNIQUE INDEX meetings_one_per_recording ON meetings (recording_id) WHERE deleted_at IS NULL;
CREATE INDEX meetings_generating ON meetings (updated_at) WHERE state = 'generating';

CREATE TABLE meeting_pins (
    id          uuid PRIMARY KEY,
    meeting_id  uuid NOT NULL REFERENCES meetings (id),
    at_ms       bigint NOT NULL CHECK (at_ms >= 0),
    note        text,
    created_at  timestamptz NOT NULL DEFAULT now(),
    deleted_at  timestamptz
);
CREATE INDEX meeting_pins_meeting ON meeting_pins (meeting_id);

CREATE TABLE meeting_chunks (
    id          uuid PRIMARY KEY,
    meeting_id  uuid NOT NULL REFERENCES meetings (id),
    kind        text NOT NULL CHECK (kind IN ('summary', 'action', 'decision', 'question', 'ask')),
    idx         integer NOT NULL CHECK (idx >= 0),
    text        text NOT NULL,
    embedding   vector(1536) NOT NULL,
    deleted_at  timestamptz
);
CREATE INDEX meeting_chunks_meeting ON meeting_chunks (meeting_id);

ALTER TABLE shares ADD COLUMN type text NOT NULL DEFAULT 'video'
    CHECK (type IN ('video', 'audio', 'meeting'));

ALTER TABLE usage_daily ADD COLUMN qa_questions integer NOT NULL DEFAULT 0;
ALTER TABLE usage_daily ADD COLUMN search_queries integer NOT NULL DEFAULT 0;
"""


def upgrade() -> None:
    # asyncpg runs one statement per call.
    for statement in UPGRADE.split(";")[:-1]:
        op.execute(statement)


def downgrade() -> None:
    op.execute("ALTER TABLE usage_daily DROP COLUMN search_queries")
    op.execute("ALTER TABLE usage_daily DROP COLUMN qa_questions")
    op.execute("ALTER TABLE shares DROP COLUMN type")
    op.execute("DROP TABLE meeting_chunks")
    op.execute("DROP TABLE meeting_pins")
    op.execute("DROP TABLE meetings")
    op.execute("ALTER TABLE recordings DROP COLUMN recording_type")
