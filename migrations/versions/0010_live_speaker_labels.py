"""A speaker's label is unique among a recording's live speakers only (TRX-11): one merged away keeps its label for an
undo, and a new speaker the Mac numbers after the live ones may take it.

Revision ID: 0010_live_speaker_labels
Revises: 0009_regenerate_cap
"""

from alembic import op

revision = "0010_live_speaker_labels"
down_revision = "0009_regenerate_cap"

UPGRADE = """
ALTER TABLE speakers DROP CONSTRAINT speakers_recording_id_label_key;
CREATE UNIQUE INDEX speakers_live_label ON speakers (recording_id, label) WHERE deleted_at IS NULL;
"""

# A deleted speaker whose label a live one took again can't keep it under the old constraint.
DOWNGRADE = """
UPDATE speakers SET label = label || ' (' || id || ')'
    WHERE deleted_at IS NOT NULL
    AND EXISTS (
        SELECT 1 FROM speakers live
        WHERE live.recording_id = speakers.recording_id AND live.label = speakers.label AND live.id <> speakers.id
    );
DROP INDEX speakers_live_label;
ALTER TABLE speakers ADD CONSTRAINT speakers_recording_id_label_key UNIQUE (recording_id, label);
"""


def upgrade() -> None:
    for statement in UPGRADE.split(";")[:-1]:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE.split(";")[:-1]:
        op.execute(statement)
