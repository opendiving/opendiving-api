"""Which models hard-delete, read off the models themselves.

`test_hard_delete.py` and `test_admin_config.py` both need this set, and what they used to
have was a copy of it hand-written in each. FastCRUD's `.delete()` branches on whether a
model carries `SoftDeleteMixin` - it flags `is_deleted` when it does and issues a real
`DELETE FROM` when it does not - so the mixin is the whole predicate, and asking the models
is the only account of it that cannot fall behind them.

Importing `app.models` is all this needs; no database, no `Settings` beyond what importing
the package already requires.
"""

import inspect

from src.app import models
from src.app.core.db.database import Base
from src.app.core.db.models import SoftDeleteMixin


def declared_models() -> set[type[Base]]:
    """Every mapped class `app.models` exports.

    `models/__init__.py` is the list, and it has to be: a model missing from it is missing
    from `Base.metadata` too, which `test_migrations.py` already fails on.
    """
    return {obj for obj in vars(models).values() if inspect.isclass(obj) and issubclass(obj, Base) and obj is not Base}


def soft_deleting_models() -> set[type[Base]]:
    return {model for model in declared_models() if issubclass(model, SoftDeleteMixin)}


def hard_deleting_models() -> set[type[Base]]:
    return declared_models() - soft_deleting_models()


def diver_owned_hard_deleted() -> set[type[Base]]:
    """The hard-deleting models that are one diver's own, addressable resource.

    `user_id` is what makes a row one diver's rather than everybody's - it excludes the
    species catalog, whose rows are shared and whose deletion is a different argument
    entirely. `uuid` is what makes it addressable: a model without one is reached through
    its parent (the join tables, `user_dive_stats`) rather than by a route of its own, so
    there is no `DELETE /thing/{uuid}` for a case to exercise.

    Together they are narrow enough that the residue is nameable, which is the point -
    `test_hard_delete.py` names every member, and a new one arriving in the set fails
    there rather than passing unnoticed.
    """
    return {
        model
        for model in hard_deleting_models()
        if "user_id" in model.__table__.columns and "uuid" in model.__table__.columns
    }
