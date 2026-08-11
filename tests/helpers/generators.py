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
