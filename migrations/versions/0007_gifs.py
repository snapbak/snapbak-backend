"""GIF share links (GIFS-AND-REPLAY-60, G7): a GIF sidecar shares as its own recording of type `gif`, uploaded
as-is with the same share lifecycle as a video's.

Revision ID: 0007_gifs
Revises: 0006_meetings
"""

from alembic import op

revision = "0007_gifs"
down_revision = "0006_meetings"

UPGRADE = """
ALTER TABLE recordings DROP CONSTRAINT recordings_recording_type_check;
ALTER TABLE recordings ADD CONSTRAINT recordings_recording_type_check
    CHECK (recording_type IN ('video', 'audio', 'gif'));

ALTER TABLE uploads DROP CONSTRAINT uploads_kind_check;
ALTER TABLE uploads ADD CONSTRAINT uploads_kind_check
    CHECK (kind IN ('audio', 'share', 'gif'));

ALTER TABLE shares DROP CONSTRAINT shares_type_check;
ALTER TABLE shares ADD CONSTRAINT shares_type_check
    CHECK (type IN ('video', 'audio', 'meeting', 'gif'));
"""

def upgrade() -> None:
    for statement in UPGRADE.split(";")[:-1]:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE.split(";")[:-1]:
        op.execute(statement)


DOWNGRADE = """
DELETE FROM uploads WHERE kind = 'gif';
ALTER TABLE uploads DROP CONSTRAINT uploads_kind_check;
ALTER TABLE uploads ADD CONSTRAINT uploads_kind_check
    CHECK (kind IN ('audio', 'share'));

DELETE FROM shares WHERE recording_id IN (SELECT id FROM recordings WHERE recording_type = 'gif');
DELETE FROM recordings WHERE recording_type = 'gif';
ALTER TABLE shares DROP CONSTRAINT shares_type_check;
ALTER TABLE shares ADD CONSTRAINT shares_type_check
    CHECK (type IN ('video', 'audio', 'meeting'));
ALTER TABLE recordings DROP CONSTRAINT recordings_recording_type_check;
ALTER TABLE recordings ADD CONSTRAINT recordings_recording_type_check
    CHECK (recording_type IN ('video', 'audio'));
"""
