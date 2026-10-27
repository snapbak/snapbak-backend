"""The speaker identification loop (MEETINGS.md D42, MOM-08): a name it guessed from what was said is `guessed`,
each recording keeps the loop's state and changes, so the app can show, review and undo them, and a Studio job waits
in stage `identifying` while its loop runs.

Revision ID: 0008_speaker_pass
Revises: 0007_gifs
"""

from alembic import op

revision = "0008_speaker_pass"
down_revision = "0007_gifs"

UPGRADE = """
ALTER TABLE speakers DROP CONSTRAINT speakers_name_source_check;
ALTER TABLE speakers ADD CONSTRAINT speakers_name_source_check
    CHECK (name_source IN ('vendor', 'user', 'guessed'));

ALTER TABLE recordings ADD COLUMN speaker_pass jsonb;

ALTER TABLE transcription_jobs DROP CONSTRAINT transcription_jobs_stage_check;
ALTER TABLE transcription_jobs ADD CONSTRAINT transcription_jobs_stage_check
    CHECK (stage IN ('transcribing', 'diarizing', 'merging', 'identifying'));
"""

DOWNGRADE = """
UPDATE transcription_jobs SET stage = 'merging' WHERE stage = 'identifying';
ALTER TABLE transcription_jobs DROP CONSTRAINT transcription_jobs_stage_check;
ALTER TABLE transcription_jobs ADD CONSTRAINT transcription_jobs_stage_check
    CHECK (stage IN ('transcribing', 'diarizing', 'merging'));

ALTER TABLE recordings DROP COLUMN speaker_pass;

UPDATE speakers SET name_source = 'vendor' WHERE name_source = 'guessed';
ALTER TABLE speakers DROP CONSTRAINT speakers_name_source_check;
ALTER TABLE speakers ADD CONSTRAINT speakers_name_source_check
    CHECK (name_source IN ('vendor', 'user'));
"""


def upgrade() -> None:
    for statement in UPGRADE.split(";")[:-1]:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE.split(";")[:-1]:
        op.execute(statement)
