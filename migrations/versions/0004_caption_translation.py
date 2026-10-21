"""caption_tracks.source_language and usage_daily.translated_lines, the caption translation's daily limit
(16-caption-translation LLD, Data).

Revision ID: 0004_caption_translation
Revises: 0003_share_video_size
"""

from alembic import op

revision = "0004_caption_translation"
down_revision = "0003_share_video_size"


def upgrade() -> None:
    op.execute("ALTER TABLE caption_tracks ADD COLUMN source_language text NOT NULL DEFAULT ''")
    op.execute(
        "ALTER TABLE usage_daily ADD COLUMN translated_lines integer NOT NULL DEFAULT 0 CHECK (translated_lines >= 0)"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE usage_daily DROP COLUMN translated_lines")
    op.execute("ALTER TABLE caption_tracks DROP COLUMN source_language")
