"""add dive_conditions

Revision ID: 25ba09ac7bf5
Revises: e63f3cba96c8
Create Date: 2026-10-23 22:40:00.000000

A table and nothing else: no row is created, so every existing dive is pending the day an
operator names `DIVE_CONDITIONS_PROVIDER`, and the worker drains the backlog at its budget's
rate. Nothing stored changes shape. `downgrade()` drops the table, and with it the fetched
records and the marks on the dive fields they filled - the filled values themselves stay on
the dives, unmarked.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "25ba09ac7bf5"
down_revision: str | None = "e63f3cba96c8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "dive_conditions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("dive_id", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.String(length=64), nullable=True),
        sa.Column("derivation_version", sa.Integer(), server_default="0", nullable=False),
        sa.Column("input_start_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("input_utc_offset_minutes", sa.Integer(), nullable=True),
        sa.Column("input_start_date_only", sa.Boolean(), nullable=True),
        sa.Column("input_latitude", sa.Float(), nullable=True),
        sa.Column("input_longitude", sa.Float(), nullable=True),
        sa.Column("input_water_type", sa.String(length=32), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("deferrals", sa.Integer(), server_default="0", nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("provider", sa.String(length=32), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("utc_offset_minutes", sa.Integer(), nullable=True),
        sa.Column("latitude", sa.Float(), nullable=True),
        sa.Column("longitude", sa.Float(), nullable=True),
        sa.Column("weather_model", sa.String(length=32), nullable=True),
        sa.Column("air_temperature", sa.Float(), nullable=True),
        sa.Column("relative_humidity", sa.Float(), nullable=True),
        sa.Column("wind_speed", sa.Float(), nullable=True),
        sa.Column("wind_direction", sa.Float(), nullable=True),
        sa.Column("wind_gusts", sa.Float(), nullable=True),
        sa.Column("cloud_cover", sa.Float(), nullable=True),
        sa.Column("precipitation", sa.Float(), nullable=True),
        sa.Column("pressure", sa.Float(), nullable=True),
        sa.Column("weather_code", sa.Integer(), nullable=True),
        sa.Column("sea_model", sa.String(length=32), nullable=True),
        sa.Column("sea_level_height", sa.Float(), nullable=True),
        sa.Column("tide_state", sa.String(length=16), nullable=True),
        sa.Column("previous_extreme_kind", sa.String(length=8), nullable=True),
        sa.Column("previous_extreme_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("previous_extreme_height", sa.Float(), nullable=True),
        sa.Column("next_extreme_kind", sa.String(length=8), nullable=True),
        sa.Column("next_extreme_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_extreme_height", sa.Float(), nullable=True),
        sa.Column("sea_surface_temperature", sa.Float(), nullable=True),
        sa.Column("wave_height", sa.Float(), nullable=True),
        sa.Column("wave_period", sa.Float(), nullable=True),
        sa.Column("swell_wave_height", sa.Float(), nullable=True),
        sa.Column("filled_air_temperature", sa.Float(), nullable=True),
        sa.Column("filled_weather", sa.String(length=32), nullable=True),
        sa.Column("filled_waves", sa.String(length=32), nullable=True),
        sa.CheckConstraint(
            "state <> 'fetched' OR (provider IS NOT NULL AND fetched_at IS NOT NULL AND at IS NOT NULL "
            "AND utc_offset_minutes IS NOT NULL AND latitude IS NOT NULL AND longitude IS NOT NULL)",
            name="ck_dive_conditions_fetched_complete",
        ),
        sa.CheckConstraint("state IN ('fetched', 'skipped', 'failed')", name="ck_dive_conditions_state"),
        sa.ForeignKeyConstraint(["dive_id"], ["dive.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("dive_id", name="ux_dive_conditions_dive_id"),
    )


def downgrade() -> None:
    op.drop_table("dive_conditions")
