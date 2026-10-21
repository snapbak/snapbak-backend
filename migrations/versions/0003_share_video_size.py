"""uploads.duration_ms, width and height: a share video's length and size, sent with its upload and taken by its share
on complete, so a newer create can't relabel an older video (12 LLD, the share branch of complete).

Revision ID: 0003_share_video_size
Revises: 0002_shares
"""

from alembic import op

revision = "0003_share_video_size"
down_revision = "0002_shares"


def upgrade() -> None:
    op.execute(
        "ALTER TABLE uploads ADD COLUMN duration_ms integer, ADD COLUMN width integer, ADD COLUMN height integer"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE uploads DROP COLUMN duration_ms, DROP COLUMN width, DROP COLUMN height")
