"""contacts, people, courses, gear items and trips keep a list of links

Revision ID: 28d1ff486573
Revises: a8c9743e21b0
Create Date: 2026-11-26 09:18:02.079135

Each of the five tables gains `links`, a JSON list of `{"url", "label"}` objects, empty on every
existing row but a contact's: a contact holding a `website` gets that address, as stored, as its
only link with no label, in one statement. Every stored website already passed the check the
column arrived with.

`contact.website` stays on the table and leaves the mapper, and the previous build still reads
it. Two triggers keep it the first link's address for as long as it stands: one on an insert
that carries links, so a contact the previous build creates keeps the website it wrote, and one
on any update naming `links`, which the previous build never sends. The revision that drops the
column drops the triggers and their function first.

`downgrade()` drops the triggers, the function and the five columns, which discards every link
but a contact's first, the one `website` holds.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "28d1ff486573"
down_revision: str | None = "a8c9743e21b0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = ("contact", "person", "course", "gear_item", "trip")


def upgrade() -> None:
    for table in _TABLES:
        op.add_column(table, sa.Column("links", sa.JSON(), server_default="[]", nullable=False))
    op.execute(
        "UPDATE contact SET links = json_build_array(json_build_object('url', website, 'label', NULL)) "
        "WHERE website IS NOT NULL"
    )
    op.execute(
        """
        CREATE FUNCTION contact_website_mirror() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            NEW.website := NEW.links -> 0 ->> 'url';
            RETURN NEW;
        END
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER contact_website_mirror_insert BEFORE INSERT ON contact FOR EACH ROW "
        "WHEN (json_array_length(NEW.links) > 0) EXECUTE FUNCTION contact_website_mirror()"
    )
    op.execute(
        "CREATE TRIGGER contact_website_mirror_update BEFORE UPDATE OF links ON contact FOR EACH ROW "
        "EXECUTE FUNCTION contact_website_mirror()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER contact_website_mirror_update ON contact")
    op.execute("DROP TRIGGER contact_website_mirror_insert ON contact")
    op.execute("DROP FUNCTION contact_website_mirror()")
    for table in reversed(_TABLES):
        op.drop_column(table, "links")
