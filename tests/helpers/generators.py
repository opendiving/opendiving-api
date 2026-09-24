from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import update
from sqlalchemy.orm import Session
from uuid6 import uuid7

from src.app import models
from src.app.models.user import USER_AVATAR_SHA256, USER_AVATAR_STORAGE_KEY, user_table
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


def create_user_picture(
    db: Session, user: models.User, *, kind: str = "avatar", with_original: bool = True
) -> models.UserPicture:
    """A picture row naming keys nothing has written: a rendition, and an original with a
    square crop unless `with_original` is off."""
    key_kind = "user-avatars" if kind == "avatar" else "user-portraits"
    picture = models.UserPicture(
        user_id=user.id,
        kind=kind,
        rendition_storage_key=f"{key_kind}/aa/{uuid7()}_{'a' * 64}",
        rendition_sha256="a" * 64,
    )
    if with_original:
        picture.original_storage_key = f"{key_kind}/bb/{uuid7()}_{'b' * 64}"
        picture.original_sha256 = "b" * 64
        picture.original_byte_size = 1
        picture.original_content_type = "image/jpeg"
        picture.original_filename = "me.jpg"
        picture.crop_x, picture.crop_y, picture.crop_width, picture.crop_height = 0, 0, 9, 9
    return _persist(db, picture)


def set_avatar_columns(db: Session, user: models.User, *, key: str | None, sha256: str | None) -> None:
    """Write the `user` row's avatar columns, which are off the mapper."""
    db.execute(
        update(user_table)
        .where(user_table.c.id == user.id)
        .values({USER_AVATAR_STORAGE_KEY: key, USER_AVATAR_SHA256: sha256})
    )
    db.commit()


def create_dive_site(db: Session, user: models.User) -> models.DiveSite:
    """A dive site of this user's, named uniquely for the same reason `create_user` is:
    these rows persist in the suite's own database and nothing removes them."""
    return _persist(
        db,
        models.DiveSite(
            user_id=user.id,
            name=f"Pescador {uuid7().hex[-8:]}",
            location_name="Moalboal, Philippines",
            notes="",
        ),
    )


def create_trip(db: Session, user: models.User) -> models.Trip:
    """A trip with one dated part, which is the shape the migration gives every trip that
    had no place - and the shape every caller here was getting when a trip held its own
    dates."""
    trip = _persist(db, models.Trip(user_id=user.id, name=f"Visayas {uuid7().hex[-8:]}", notes=""))
    _persist(db, models.TripPart(trip_id=trip.id, position=0, start_date=date(2026, 6, 1)))
    return trip


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


def create_course(
    db: Session,
    user: models.User,
    *,
    start_date: date | None = date(2026, 3, 2),
    end_date: date | None = None,
    agency: str | None = "tdi",
    status: str = "completed",
) -> models.Course:
    """A training course of this user's.

    Uniquely named like the rest of these, though for a weaker reason than most: `course`
    carries **no** per-user unique index (a course retaken later is legitimately the same
    name twice), so this is only about telling one run's fixture rows from the last run's
    rather than about avoiding a collision the API would refuse.

    `start_date` is settable and nullable because a dateless course is the case
    `_LIST_ORDER` in `crud_courses` exists for - the `NULLS LAST` half of that ordering has
    no other way to be exercised. `agency` is settable for the same shape of reason: a
    course that ran under none is a state only this column can hold, `certification.agency`
    staying `NOT NULL`.

    `end_date` and `status` are settable for `GET /courses`' filters: an interval only has
    two ends if both are writable, and the whole point of the date window is the course
    whose two dates straddle its edge. `end_date` defaults to `None` rather than to a day
    after `start_date`, so an ongoing course stays the cheap fixture to ask for.
    """
    return _persist(
        db,
        models.Course(
            user_id=user.id,
            name=f"Advanced Nitrox {uuid7().hex[-8:]}",
            agency=agency,
            status=status,
            start_date=start_date,
            end_date=end_date,
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
            # A real `ServiceKind` member, which "inspection" was not. Nothing asserted on
            # the old string, and every write path validates this column through that enum -
            # so a fixture row carrying a value outside it was one no API call could have
            # produced, and one the DiveJSON writer refuses outright (`ExportGearServiceSchedule.type`
            # is the enum). It only ever surfaced once a test exported a generator-seeded
            # schedule, which is what `test_logbook_import.py` does.
            kind="visual_inspection",
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
            kind="visual_inspection",
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


def create_dive_form_preset(db: Session, user: models.User) -> models.DiveFormPreset:
    """A dive form preset of this user's. Uniquely named for the same two reasons
    `create_gear_set` is: these rows persist in the suite's own database, and
    `dive_form_preset_name_exists` treats names as unique per user.

    Deliberately not one of the three seeded defaults - a fixture called "Basic" would make
    every restore test's "add what is missing" arithmetic depend on which builder ran first.
    """
    return _persist(
        db,
        models.DiveFormPreset(
            user_id=user.id,
            name=f"Warm water {uuid7().hex[-8:]}",
            hidden_fields=["altitude", "mixture.po2_limit"],
        ),
    )


def create_species(db: Session, *, aphia_id: int | None = None, **overrides: Any) -> models.Species:
    """A catalog row.

    No `user` parameter, unlike every other builder here, and that is the whole point: the
    species catalog is global, so a species belongs to nobody and is shared by every account.

    `aphia_id` is `unique=True` and these rows persist in the suite's own database with
    nothing cleaning them up, so it is drawn from uuid7's random tail by default for the same
    reason `unique_username` is - a fixed fixture id collides on the second run. Pass one
    explicitly when a test is *about* the id.

    **The name is deliberately not a plausible taxon**, which matters more here than for any
    other generator in this file. Every other one writes rows scoped to a fixture `user_id`,
    so they are invisible to any other account; `species` is global, so a fixture row is in
    the dive form's picker for *every* account in whatever database it landed in. That used
    to be the developer's own - an earlier version wrote `Amphiprion <hex>` and then
    `Testudo fixtura <hex>`, both real genera, and a developer searching "amphiprion" got a
    screenful of test data. The suite has its own database now, so the blast radius is one
    disposable database rather than the dev app, but the naming rule stands: `zzfixture`
    cannot be reached by any query a diver would type, and sorts last if it ever is.
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


def create_dive_recording(db: Session, user: models.User, dive: models.Dive, *, ordinal: int = 0) -> Any:
    """One recording of a dive, to hang a file or a profile off.

    Exists because `dive_file.recording_id` and `dive_profile.recording_id` are `NOT NULL`:
    a test seeding either has to seed one of these first, and doing it by hand at every such
    site is how the ordinal and the owner drift apart.
    """
    return _persist(
        db,
        models.DiveRecording(dive_id=dive.id, user_id=user.id, ordinal=ordinal, start_time=dive.start_time),
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
