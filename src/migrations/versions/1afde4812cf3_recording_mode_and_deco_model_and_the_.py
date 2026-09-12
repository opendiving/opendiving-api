"""a recording records its mode and deco model, and a profile its six deco extremes

The storage half of DiveJSON's decompression members. A **recording** gains the mode its
device ran in and the five columns of the model it ran - both are the device's rather than
the dive's, because two computers on one dive give two answers to each and a backup run in
gauge mode does not make the dive a gauge dive. A **profile** gains one summary extreme per
new channel, which is what `channels` on the dive read is derived from; the samples
themselves go into the existing `data` payload and need no column.

`ck_dive_recording_deco_gf_low_within_high` is written by hand rather than by autogenerate,
which did not detect it. It needs no repair pass, unlike `ck_dive_avg_depth_within_max`:
every one of these columns is new and no row can yet hold a value, let alone an inverted
pair.

**Nothing backfills the profile channels.** `PROFILE_EXTRACTOR_VERSION` goes to 4 in the
same change, so `backfill_dive_profiles` would re-extract every file-backed profile on its
next run - and no part of this change runs it. Existing profiles keep the four channels they
have until their recording is re-uploaded. See *"The decompression channels arrive for new
dives only"* in DECISIONS.md; a data pass here would also be the wrong place for it, since a
migration cannot read a blob store.

`downgrade()` drops the columns, which discards what they held - by construction, since
nothing else in the schema carries a mode or a model.

Revision ID: 1afde4812cf3
Revises: f9d04a823776
Create Date: 2026-09-12 15:39:43.125296

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "1afde4812cf3"
down_revision: str | None = "f9d04a823776"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_GF_ORDER_CONSTRAINT = "ck_dive_recording_deco_gf_low_within_high"


def upgrade() -> None:
    # One summary extreme per new channel, all nullable: NULL is "this profile has no such
    # channel", which is what the read derives `channels` from, and a stored `0` is a real
    # reading. Which extreme is per quantity - see the columns' comments in
    # `models/dive_profile.py`.
    op.add_column("dive_profile", sa.Column("min_ndl_s", sa.Integer(), nullable=True))
    op.add_column("dive_profile", sa.Column("max_tts_s", sa.Integer(), nullable=True))
    op.add_column("dive_profile", sa.Column("max_ppo2_bar100", sa.Integer(), nullable=True))
    op.add_column("dive_profile", sa.Column("max_cns_pct10", sa.Integer(), nullable=True))
    op.add_column("dive_profile", sa.Column("max_gradient_factor_pct", sa.Integer(), nullable=True))
    op.add_column("dive_profile", sa.Column("max_surface_gradient_factor_pct", sa.Integer(), nullable=True))

    # A closed vocabulary with no `CHECK`, the pattern `dive.water_type` and `gear_item.type`
    # follow: the write schemas hold the vocabulary and the read schemas widen to a string,
    # so one unrecognized row cannot fail a whole dive read.
    op.add_column("dive_recording", sa.Column("mode", sa.String(length=32), nullable=True))
    op.add_column("dive_recording", sa.Column("deco_algorithm", sa.String(length=32), nullable=True))
    op.add_column("dive_recording", sa.Column("deco_name", sa.String(length=64), nullable=True))
    op.add_column("dive_recording", sa.Column("deco_gf_low", sa.Integer(), nullable=True))
    op.add_column("dive_recording", sa.Column("deco_gf_high", sa.Integer(), nullable=True))
    op.add_column("dive_recording", sa.Column("deco_conservatism", sa.Integer(), nullable=True))

    # The backstop under two write paths that already drop an inverted pair - the parsers in
    # `ParsedDecoModel`, the importer in its planner - rather than the only place the rule
    # exists. A NULL on either side passes: the pair is both-or-neither, and that is the
    # writers' rule rather than something a `CHECK` can express.
    op.create_check_constraint(
        _GF_ORDER_CONSTRAINT,
        "dive_recording",
        "deco_gf_low IS NULL OR deco_gf_high IS NULL OR deco_gf_low <= deco_gf_high",
    )


def downgrade() -> None:
    op.drop_constraint(_GF_ORDER_CONSTRAINT, "dive_recording", type_="check")
    op.drop_column("dive_recording", "deco_conservatism")
    op.drop_column("dive_recording", "deco_gf_high")
    op.drop_column("dive_recording", "deco_gf_low")
    op.drop_column("dive_recording", "deco_name")
    op.drop_column("dive_recording", "deco_algorithm")
    op.drop_column("dive_recording", "mode")
    op.drop_column("dive_profile", "max_surface_gradient_factor_pct")
    op.drop_column("dive_profile", "max_gradient_factor_pct")
    op.drop_column("dive_profile", "max_cns_pct10")
    op.drop_column("dive_profile", "max_ppo2_bar100")
    op.drop_column("dive_profile", "max_tts_s")
    op.drop_column("dive_profile", "min_ndl_s")
