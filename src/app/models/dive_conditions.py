from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, Float, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ..core.db.database import Base


class DiveConditions(Base):
    """What a provider said about the surface at a dive's place and hour, and what of it was
    written into the dive (`services.dive_conditions`).

    **One row per dive, and its absence is a state**: a dive with no row is pending, which is
    how every existing dive becomes a candidate without a backfill. `state` says which of the
    other three it is in - `fetched` (the provider answered), `skipped` (nothing to ask with:
    no instant, or no position) or `failed` (asked, and the asking was the dive's fault, or
    deferred after repeated instance-wide failures; see `next_attempt_at`).

    The `input_*` columns are the dive's inputs the row was computed from, copied raw, so the
    worker can tell a row that no longer describes its dive by comparing them with the dive's
    current ones (`IS DISTINCT FROM`). The position is stored rounded exactly as the request
    rounds it, by the same SQL expression the comparison uses.

    The `filled_*` columns are the values the fetch wrote into the dive's own fields, null
    where it wrote nothing. Their presence is the field's "fetched" mark: a sweep compares a
    field with what was written there, never with a re-derivation, so a later change to the
    weather table or the wave thresholds cannot clear every mark at once - `derivation_version`
    re-candidates the row instead.

    The weather block is present when `weather_model` is, the sea block when `sea_model` is;
    every value inside either may be null where the provider had none for that hour.
    """

    __tablename__ = "dive_conditions"

    id: Mapped[int] = mapped_column(autoincrement=True, nullable=False, primary_key=True, init=False)
    dive_id: Mapped[int] = mapped_column(ForeignKey("dive.id", ondelete="CASCADE"))
    state: Mapped[str] = mapped_column(String(16))
    # Why a row is skipped or failed, as a short code. Never a URL or a provider's message.
    reason: Mapped[str | None] = mapped_column(String(64), default=None)
    derivation_version: Mapped[int] = mapped_column(Integer, default=0, server_default="0")

    input_start_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    input_utc_offset_minutes: Mapped[int | None] = mapped_column(Integer, default=None)
    input_start_date_only: Mapped[bool | None] = mapped_column(Boolean, default=None)
    input_latitude: Mapped[float | None] = mapped_column(Float, default=None)
    input_longitude: Mapped[float | None] = mapped_column(Float, default=None)
    input_water_type: Mapped[str | None] = mapped_column(String(32), default=None)

    # A `failed` row's retry bookkeeping. `attempts` counts failures that were the dive's
    # doing and stops at a cap, after which `next_attempt_at` is null and only an input change
    # asks again; `deferrals` counts instance-wide failures that pushed the dive back, which
    # never count toward that cap.
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    deferrals: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    provider: Mapped[str | None] = mapped_column(String(32), default=None)
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    # The instant the record describes and the offset it was resolved at - the dive's own, or
    # for a dive whose offset is unknown, the one its place's zone had on that date.
    at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    utc_offset_minutes: Mapped[int | None] = mapped_column(Integer, default=None)
    # Where the provider was asked about, rounded as the request was. The same as the input
    # position on a row the worker wrote; an imported row keeps its document's position even
    # where its inputs are left null for the worker to find stale.
    latitude: Mapped[float | None] = mapped_column(Float, default=None)
    longitude: Mapped[float | None] = mapped_column(Float, default=None)

    weather_model: Mapped[str | None] = mapped_column(String(32), default=None)
    air_temperature: Mapped[float | None] = mapped_column(Float, default=None)
    relative_humidity: Mapped[float | None] = mapped_column(Float, default=None)
    wind_speed: Mapped[float | None] = mapped_column(Float, default=None)
    wind_direction: Mapped[float | None] = mapped_column(Float, default=None)
    wind_gusts: Mapped[float | None] = mapped_column(Float, default=None)
    cloud_cover: Mapped[float | None] = mapped_column(Float, default=None)
    precipitation: Mapped[float | None] = mapped_column(Float, default=None)
    pressure: Mapped[float | None] = mapped_column(Float, default=None)
    weather_code: Mapped[int | None] = mapped_column(Integer, default=None)

    sea_model: Mapped[str | None] = mapped_column(String(32), default=None)
    # Above the model's mean sea level, not a chart datum.
    sea_level_height: Mapped[float | None] = mapped_column(Float, default=None)
    tide_state: Mapped[str | None] = mapped_column(String(16), default=None)
    previous_extreme_kind: Mapped[str | None] = mapped_column(String(8), default=None)
    previous_extreme_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    previous_extreme_height: Mapped[float | None] = mapped_column(Float, default=None)
    next_extreme_kind: Mapped[str | None] = mapped_column(String(8), default=None)
    next_extreme_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    next_extreme_height: Mapped[float | None] = mapped_column(Float, default=None)
    sea_surface_temperature: Mapped[float | None] = mapped_column(Float, default=None)
    wave_height: Mapped[float | None] = mapped_column(Float, default=None)
    wave_period: Mapped[float | None] = mapped_column(Float, default=None)
    swell_wave_height: Mapped[float | None] = mapped_column(Float, default=None)

    filled_air_temperature: Mapped[float | None] = mapped_column(Float, default=None)
    filled_weather: Mapped[str | None] = mapped_column(String(32), default=None)
    filled_waves: Mapped[str | None] = mapped_column(String(32), default=None)

    __table_args__ = (
        # One row per dive, which is also what makes two runs answering one dive a lost race
        # rather than a second row.
        UniqueConstraint("dive_id", name="ux_dive_conditions_dive_id"),
        CheckConstraint("state IN ('fetched', 'skipped', 'failed')", name="ck_dive_conditions_state"),
        # What every reader of a `fetched` row relies on being there.
        CheckConstraint(
            "state <> 'fetched' OR (provider IS NOT NULL AND fetched_at IS NOT NULL AND at IS NOT NULL "
            "AND utc_offset_minutes IS NOT NULL AND latitude IS NOT NULL AND longitude IS NOT NULL)",
            name="ck_dive_conditions_fetched_complete",
        ),
    )
