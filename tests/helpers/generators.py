from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session
from uuid6 import uuid7

from src.app import models
from tests.conftest import fake, unique_email, unique_username


def create_user(db: Session, is_super_user: bool = False) -> models.User:
    _user = models.User(
        name=fake.name(),
        # 48 bits of CSPRNG entropy rather than a name from faker's small vocabulary: this
        # writes a real row to the developer's database and nothing cleans it up, so the
        # namespace only ever fills up. See `unique_username`.
        username=unique_username(),
        email=unique_email(),
        profile_image_url=fake.image_url(),
        uuid=uuid7(),
        is_superuser=is_super_user,
    )

    db.add(_user)
    db.commit()
    db.refresh(_user)

    return _user


def _persist[RowT](db: Session, row: RowT) -> RowT:
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def create_dive_site(db: Session, user: models.User, *, is_deleted: bool = False) -> models.DiveSite:
    """A dive site of this user's, named uniquely for the same reason `create_user` is:
    these rows go into the developer's own database and nothing removes them."""
    return _persist(
        db,
        models.DiveSite(
            user_id=user.id,
            name=f"Pescador {uuid7().hex[-8:]}",
            location="Moalboal",
            notes="",
            is_deleted=is_deleted,
        ),
    )


def create_trip(db: Session, user: models.User, *, is_deleted: bool = False) -> models.Trip:
    return _persist(
        db,
        models.Trip(
            user_id=user.id,
            name=f"Visayas {uuid7().hex[-8:]}",
            start_date=date(2026, 6, 1),
            notes="",
            is_deleted=is_deleted,
        ),
    )


def create_gear_item(
    db: Session, user: models.User, *, is_deleted: bool = False, is_archived: bool = False
) -> models.GearItem:
    """A gear item of this user's. Uniquely named for the same reason as the rest, and
    doubly so here: `ux_gear_item_user_id_brand_name_lower` is a real unique index over
    (user, brand, name) that a repeated fixture name would collide on."""
    return _persist(
        db,
        models.GearItem(
            user_id=user.id,
            name=f"MK25 {uuid7().hex[-8:]}",
            brand="Scubapro",
            type="regulator",
            notes="",
            is_deleted=is_deleted,
            is_archived=is_archived,
        ),
    )


def create_gear_set(db: Session, user: models.User, *, is_deleted: bool = False) -> models.GearSet:
    """A gear set of this user's. Uniquely named like the rest, and for one extra reason:
    `gear_set_name_exists` treats names as unique per user, so a fixture reusing one would
    be a duplicate the API would refuse to create."""
    return _persist(
        db,
        models.GearSet(
            user_id=user.id,
            name=f"Wreck kit {uuid7().hex[-8:]}",
            is_deleted=is_deleted,
        ),
    )


def create_dive(
    db: Session, user: models.User, *, trip: models.Trip | None = None, is_deleted: bool = False
) -> models.Dive:
    return _persist(
        db,
        models.Dive(
            user_id=user.id,
            trip_id=trip.id if trip is not None else None,
            dive_number=1,
            start_time=datetime(2026, 6, 1, 9, 0, tzinfo=UTC),
            duration=1800,
            notes="",
            is_deleted=is_deleted,
        ),
    )


# Every dive seeded by `create_dive_log` hangs off this instant, so a test reads as "day 3
# of the log" rather than as a date. Fixed rather than `now()`-relative: the services these
# tests exercise slice a log by time, and a suite that quietly means something different
# each day it runs is the wrong tool for testing that.
#
# Deliberately unrelated to the 2026 dates `create_dive`/`create_trip` use, and two years
# clear of them: a module mixing the two builders gets dives that sort unambiguously into a
# "log" half and a "single row" half. If you ever need them interleaved, pass the day
# offsets rather than moving this.
LOG_EPOCH = datetime(2024, 5, 1, 9, 0, tzinfo=UTC)


def log_day(offset: int) -> datetime:
    return LOG_EPOCH + timedelta(days=offset)


def create_dive_log(
    db: Session, user: models.User, *numbered_days: tuple[int, int], **overrides: Any
) -> list[models.Dive]:
    """Seed a log from `(dive_number, day offset)` pairs and return the dives, in the order
    given.

    Separate from `create_dive` rather than layered on it: this exists to arrange a *log*
    whose ordering is the thing under test, so it takes dive numbers and days as data and
    commits the whole set at once. `overrides` reaches the model directly, which is how a
    test seeds `is_deleted`/`deleted_at` or a `utc_offset_minutes`. They apply to every
    dive in the call, so a log whose rows differ in one of them takes two calls.
    """
    dives = [
        models.Dive(
            user_id=user.id,
            dive_number=dive_number,
            start_time=log_day(day),
            duration=1800,
            notes="",
            **overrides,
        )
        for dive_number, day in numbered_days
    ]
    db.add_all(dives)
    db.commit()
    for dive in dives:
        db.refresh(dive)
    return dives
