"""The shares table and the uploads.share_id foreign key (12-share-links LLD, Postgres schema).

Revision ID: 0002_shares
Revises: 0001_cloud_transcripts
"""

from alembic import op

revision = "0002_shares"
down_revision = "0001_cloud_transcripts"

UPGRADE = """
CREATE TABLE shares (
    id             uuid PRIMARY KEY,
    recording_id   uuid NOT NULL REFERENCES recordings (id),
    slug           text NOT NULL UNIQUE CHECK (length(slug) >= 16),
    s3_key         text,
    poster_s3_key  text,
    state          text NOT NULL DEFAULT 'uploading' CHECK (state IN ('uploading', 'live', 'disabled')),
    expires_at     timestamptz,
    title          text NOT NULL CHECK (length(title) <= 300),
    version        integer NOT NULL DEFAULT 0,
    video_bytes    bigint,
    duration_ms    integer,
    width          integer,
    height         integer,
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now(),
    deleted_at     timestamptz
);
CREATE UNIQUE INDEX shares_one_per_recording ON shares (recording_id) WHERE deleted_at IS NULL;

ALTER TABLE uploads ADD CONSTRAINT uploads_share FOREIGN KEY (share_id) REFERENCES shares (id);
"""


def upgrade() -> None:
    # asyncpg runs one statement per call.
    for statement in UPGRADE.split(";")[:-1]:
        op.execute(statement)


def downgrade() -> None:
    op.execute("ALTER TABLE uploads DROP CONSTRAINT uploads_share")
    op.execute("DROP TABLE shares")
