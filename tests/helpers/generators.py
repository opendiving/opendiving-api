from datetime import UTC, date, datetime

from sqlalchemy.orm import Session
from uuid6 import uuid7  # 126

from src.app import models
from tests.conftest import fake, unique_email, unique_username


def create_user(db: Session, is_super_user: bool = False) -> models.User:
    _user = models.User(
        name=fake.name(),
        # Unique across runs, not merely unlikely to repeat: this writes a real row to
        # the developer's database and nothing cleans it up. See `unique_username`.
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
