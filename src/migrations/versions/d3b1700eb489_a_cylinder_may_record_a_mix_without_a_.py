"""a cylinder may record a mix without a vessel

Revision ID: d3b1700eb489
Revises: 3818275fcfcd
Create Date: 2026-09-05 10:47:51.619304

`dive_mixture.volume`, `.oxygen` and `.helium` become nullable, and NULL is a third state
rather than a missing value: the source never recorded it. All three are OPTIONAL in
DiveJSON, whose §6.3 blesses a cylinder converted from a mix-only source with its vessel
members absent and says of `oxygen` in as many words that absent means not recorded, **not
21** - so the app is catching up to the format it already writes. UDDF is where this
arrives from in practice: `<tankvolume>` is `minOccurs="0"` and foreign exporters routinely
omit it, and until now such a cylinder could not be stored at all. Logbook import skipped
it and reported the loss.

**The four `CHECK`s over these columns are restated by hand, and that is a restatement
rather than a change.** SQL `CHECK` passes on UNKNOWN, so `volume > 0` already admitted a
NULL and `oxygen + helium <= 100` already admitted a row with one operand missing: nothing
about which rows they accept moves here. What moves is that they now say so, like every
other nullable column on this table, instead of being the four that go quiet without
mentioning it. Autogenerate cannot see a constraint whose *text* changed under an unchanged
name (revision `b1dbbf0d263e` hit the same thing), which is why this half is written out.

Nothing to repair on the way up: every existing row has all three values, and the new
constraint text accepts everything the old text did.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d3b1700eb489"
down_revision: str | None = "3818275fcfcd"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NULLABLE_COLUMNS = ("volume", "oxygen", "helium")

_NULL_AWARE = {
    "ck_dive_mixture_volume_positive": "volume IS NULL OR volume > 0",
    "ck_dive_mixture_oxygen_range": "oxygen IS NULL OR (oxygen >= 0 AND oxygen <= 100)",
    "ck_dive_mixture_helium_range": "helium IS NULL OR (helium >= 0 AND helium <= 100)",
    "ck_dive_mixture_oxygen_helium_sum": "oxygen IS NULL OR helium IS NULL OR oxygen + helium <= 100",
}

_IMPLICIT = {
    "ck_dive_mixture_volume_positive": "volume > 0",
    "ck_dive_mixture_oxygen_range": "oxygen >= 0 AND oxygen <= 100",
    "ck_dive_mixture_helium_range": "helium >= 0 AND helium <= 100",
    "ck_dive_mixture_oxygen_helium_sum": "oxygen + helium <= 100",
}


def _restate(texts: dict[str, str]) -> None:
    for name, text in texts.items():
        op.drop_constraint(name, "dive_mixture", type_="check")
        op.create_check_constraint(name, "dive_mixture", text)


def upgrade() -> None:
    for column in _NULLABLE_COLUMNS:
        op.alter_column("dive_mixture", column, existing_type=sa.DOUBLE_PRECISION(precision=53), nullable=True)
    _restate(_NULL_AWARE)


def downgrade() -> None:
    # A cylinder that recorded no size or no mix is precisely what the old schema could not
    # hold, so it goes - which is also exactly what the import used to do with one, making
    # this a real return to the old behaviour rather than an approximation of it. The
    # alternative is to invent 11.1 L or 21 % for a diver who never recorded either, and
    # inventing those is the thing this revision exists to stop. Lossy, and deliberately so:
    # undoing a change that admitted a state has nowhere to put the rows in it.
    op.execute("DELETE FROM dive_mixture WHERE volume IS NULL OR oxygen IS NULL OR helium IS NULL")
    for column in _NULLABLE_COLUMNS:
        op.alter_column("dive_mixture", column, existing_type=sa.DOUBLE_PRECISION(precision=53), nullable=False)
    _restate(_IMPLICIT)
