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


def create_dive_site(db: Session, user: models.User) -> models.DiveSite:
    """A dive site of this user's, named uniquely for the same reason `create_user` is:
    these rows go into the developer's own database and nothing removes them."""
    return _persist(
        db,
        models.DiveSite(
            user_id=user.id,
            name=f"Pescador {uuid7().hex[-8:]}",
            location="Moalboal",
            notes="",
        ),
    )


def create_trip(db: Session, user: models.User) -> models.Trip:
    return _persist(
        db,
        models.Trip(
            user_id=user.id,
            name=f"Visayas {uuid7().hex[-8:]}",
            start_date=date(2026, 6, 1),
            notes="",
        ),
    )


def create_certification(
    db: Session,
    user: models.User,
    *,
    certified_on: date | None = None,
    course: models.Course | None = None,
) -> models.Certification:
    """A certification of this user's.

    `certified_on` defaults to `None` on purpose: a card with no date is the case
    `_LIST_ORDER` in `crud_certifications` exists for, and the fixture should make one
    without ceremony.
    """
    return _persist(
        db,
        models.Certification(
            user_id=user.id,
            agency="padi",
            name=f"Advanced Open Water {uuid7().hex[-8:]}",
            certified_on=certified_on,
            course_id=course.id if course is not None else None,
        ),
    )


def create_course(db: Session, user: models.User, *, start_date: date | None = date(2026, 3, 2)) -> models.Course:
    """A training course of this user's.

    Uniquely named like the rest of these, though for a weaker reason than most: `course`
    carries **no** per-user unique index (a course retaken later is legitimately the same
    name twice), so this is only about telling fixture rows apart in a developer's own
    database rather than about avoiding a collision the API would refuse.

    `start_date` is settable and nullable because a dateless course is the case
    `_LIST_ORDER` in `crud_courses` exists for - the `NULLS LAST` half of that ordering has
    no other way to be exercised.
    """
    return _persist(
        db,
        models.Course(
            user_id=user.id,
            name=f"Advanced Nitrox {uuid7().hex[-8:]}",
            agency="tdi",
            status="completed",
            start_date=start_date,
            notes="",
        ),
    )


def create_gear_item(db: Session, user: models.User, *, is_archived: bool = False) -> models.GearItem:
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
            is_archived=is_archived,
        ),
    )


def create_gear_service_schedule(db: Session, user: models.User, item: models.GearItem) -> models.GearServiceSchedule:
    """A service schedule on one of this user's gear items.

    `interval_months` is set because `ck_gear_service_schedule_has_an_interval` requires at
    least one of the two intervals - a schedule with neither would never come due, so the
    database refuses it.
    """
    return _persist(
        db,
        models.GearServiceSchedule(
            user_id=user.id,
            gear_item_id=item.id,
            kind="inspection",
            starts_on=date(2026, 1, 1),
            interval_months=12,
        ),
    )


def create_gear_service_record(
    db: Session,
    user: models.User,
    item: models.GearItem,
    *,
    schedule: models.GearServiceSchedule | None = None,
) -> models.GearServiceRecord:
    """A service record, optionally attached to a schedule.

    `gear_service_schedule_id` is nullable on purpose - a diver can log a service that no
    schedule was tracking - which is also the state a record lands in when its schedule is
    deleted, the FK's `ON DELETE SET NULL` clearing the column.
    """
    return _persist(
        db,
        models.GearServiceRecord(
            user_id=user.id,
            gear_item_id=item.id,
            gear_service_schedule_id=schedule.id if schedule is not None else None,
            kind="inspection",
            serviced_on=date(2026, 6, 1),
            dive_count_at_service=0,
            notes="",
        ),
    )


def create_gear_set(db: Session, user: models.User) -> models.GearSet:
    """A gear set of this user's. Uniquely named like the rest, and for one extra reason:
    `gear_set_name_exists` treats names as unique per user, so a fixture reusing one would
    be a duplicate the API would refuse to create."""
    return _persist(
        db,
        models.GearSet(
            user_id=user.id,
            name=f"Wreck kit {uuid7().hex[-8:]}",
        ),
    )


def create_species(db: Session, *, aphia_id: int | None = None, **overrides: Any) -> models.Species:
    """A catalog row.

    No `user` parameter, unlike every other builder here, and that is the whole point: the
    species catalog is global, so a species belongs to nobody and is shared by every account.

    `aphia_id` is `unique=True` and these rows go into the developer's own database with
    nothing cleaning them up, so it is drawn from uuid7's random tail by default for the same
    reason `unique_username` is - a fixed fixture id collides on the second run. Pass one
    explicitly when a test is *about* the id.

    **The name is deliberately not a plausible taxon**, which matters more here than for any
    other generator in this file. Every other one writes rows scoped to a fixture `user_id`,
    so they are invisible to a real account; `species` is global, so a fixture row shows up in
    the dive form's picker for *every* account on that instance. An earlier version of this
    used `Amphiprion <hex>` and then `Testudo fixtura <hex>` - both real genera - and a
    developer searching "amphiprion" got a screenful of test data. `zzfixture` cannot be
    reached by any query a diver would type, and sorts last if it ever is.
    """
    defaults: dict[str, Any] = {
        "aphia_id": aphia_id if aphia_id is not None else int(uuid7().hex[-7:], 16),
        "scientific_name": f"zzfixture-species-{uuid7().hex[-8:]}",
        "rank": "Species",
        "status": "accepted",
    }
    defaults.update(overrides)
    return _persist(db, models.Species(**defaults))


def create_dive(
    db: Session,
    user: models.User,
    *,
    trip: models.Trip | None = None,
    course: models.Course | None = None,
    is_deleted: bool = False,
) -> models.Dive:
    return _persist(
        db,
        models.Dive(
            user_id=user.id,
            trip_id=trip.id if trip is not None else None,
            course_id=course.id if course is not None else None,
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
