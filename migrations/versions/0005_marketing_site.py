"""The marketing site's tables: waitlist_entries, early_access_spots and contact_messages (website SPEC §6).

Revision ID: 0005_marketing_site
Revises: 0004_caption_translation
"""

from alembic import op

revision = "0005_marketing_site"
down_revision = "0004_caption_translation"

UPGRADE = """
CREATE TABLE waitlist_entries (
    id          uuid PRIMARY KEY,
    email       text NOT NULL,
    use_case    text,
    source      text NOT NULL DEFAULT 'website',
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX ix_waitlist_entries_email ON waitlist_entries (email);

CREATE TABLE early_access_spots (
    id             serial PRIMARY KEY,
    total_spots    integer NOT NULL CHECK (total_spots >= 0),
    claimed_spots  integer NOT NULL DEFAULT 0 CHECK (claimed_spots >= 0),
    updated_at     timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE contact_messages (
    id          uuid PRIMARY KEY,
    name        text NOT NULL,
    email       text NOT NULL,
    message     text NOT NULL,
    source      text NOT NULL DEFAULT 'website',
    created_at  timestamptz NOT NULL DEFAULT now()
);
"""


def upgrade() -> None:
    # asyncpg runs one statement per call.
    for statement in UPGRADE.split(";")[:-1]:
        op.execute(statement)


def downgrade() -> None:
    op.execute("DROP TABLE contact_messages")
    op.execute("DROP TABLE early_access_spots")
    op.execute("DROP TABLE waitlist_entries")
