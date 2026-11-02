"""A meeting's whole regenerates per UTC day, capped at five (MOM-09): the day counted and how many that day.

Revision ID: 0009_regenerate_cap
Revises: 0008_speaker_pass
"""

from alembic import op

revision = "0009_regenerate_cap"
down_revision = "0008_speaker_pass"

UPGRADE = """
ALTER TABLE meetings ADD COLUMN regenerate_day date;
ALTER TABLE meetings ADD COLUMN regenerate_count smallint NOT NULL DEFAULT 0;
"""

DOWNGRADE = """
ALTER TABLE meetings DROP COLUMN regenerate_count;
ALTER TABLE meetings DROP COLUMN regenerate_day;
"""


def upgrade() -> None:
    for statement in UPGRADE.split(";")[:-1]:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE.split(";")[:-1]:
        op.execute(statement)
