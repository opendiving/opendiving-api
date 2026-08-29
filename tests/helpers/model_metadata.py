"""Which models hard-delete, read off the models themselves.

`test_hard_delete.py` and `test_admin_config.py` both need this, and what they used to have
was a copy of it hand-written in each. FastCRUD's `.delete()` branches on whether a model
carries `SoftDeleteMixin` - it flags `is_deleted` when it does and issues a real
`DELETE FROM` when it does not - so the mixin is the whole predicate, and asking the models
is the only account of it that cannot fall behind them.

Needs no database, and nothing from `Settings` beyond what importing the package already
requires.
"""

import importlib
import pkgutil

from src.app import models
from src.app.core.db.database import Base
from src.app.core.db.models import SoftDeleteMixin


def _import_every_model_module() -> None:
    """Walked from disk rather than read off `models/__init__.py`, which is a list somebody
    maintains and this must not depend on.

    A model file wired into its own `crud_*` module reaches `Base.metadata` through that
    import alone, so it can be live - migrated, routed, deleting rows - while missing from
    the package's re-exports. `test_migrations.py` cannot see that: it only asks whether a
    table already *on* the metadata reached a revision, so a model absent from both is
    absent from nothing it checks. `migrations/env.py` walks the package for the same
    reason.
    """
    for _, module_name, _ in pkgutil.walk_packages(models.__path__, models.__name__ + "."):
        importlib.import_module(module_name)


def declared_models() -> set[type[Base]]:
    """Every class SQLAlchemy has actually mapped under `app.models`.

    The registry, not `vars(models)`: the registry is what the ORM itself resolves against,
    so a model is in it exactly when it is real. Scoped to the package so the set does not
    depend on what else the importing test happened to pull in - `token_blacklist` lives
    under `core/db/`, is nobody's resource, and `test_migrations.py` covers it separately.
    """
    _import_every_model_module()
    prefix = models.__name__ + "."
    return {mapper.class_ for mapper in Base.registry.mappers if mapper.class_.__module__.startswith(prefix)}


def soft_deleting_models() -> set[type[Base]]:
    return {model for model in declared_models() if issubclass(model, SoftDeleteMixin)}


def hard_deleting_models() -> set[type[Base]]:
    return declared_models() - soft_deleting_models()


def addressable_hard_deleted() -> set[type[Base]]:
    """The hard-deleting models that carry a public `uuid`.

    `uuid` rather than `user_id`, deliberately. Ownership looks like the sharper filter and
    is the one that quietly loses models: `DiveProfile` and `CertificationFile` are one
    diver's rows reached through a parent and carry no `user_id` column at all, so scoping
    on it drops them without anyone deciding to - and with them, any model later added in
    that shape. A `uuid` is what makes a row nameable from outside, which is the property
    that makes "is there a delete of its own to test?" a question worth asking; every model
    for which the answer is no is named in `NOT_A_DIVERS_OWN_RESOURCE` with the reason.
    """
    return {model for model in hard_deleting_models() if "uuid" in model.__table__.columns}


NOT_A_DIVERS_OWN_RESOURCE: dict[str, str] = {
    "AuthenticationRequest": (
        "Sign-in state, not a logbook row. Nothing offers a delete of it - rows expire and are swept "
        "on a cron - so there is no delete for a case to exercise."
    ),
    "CertificationFile": (
        "A card image reached through its certification: `DELETE /certification/{uuid}/file/{side}` "
        "owns its lifecycle, along with the files volume it writes to."
    ),
    "DiveFile": (
        "The dive's source export, reached through the dive: `DELETE /dive/{uuid}/file` owns its "
        "lifecycle, along with the files volume it writes to."
    ),
    "DiveProfile": (
        "The dive's depth samples. `GET /dive/{uuid}/profile` is the only route it has - rows go when "
        "the file that produced them goes, never on their own."
    ),
    "Species": (
        "Everybody's row rather than one diver's, and nothing in the app deletes a species by design "
        "(see `models/species.py`)."
    ),
    "UserSession": (
        "There is a `DELETE /user/session/{uuid}`, and it **revokes rather than removes**: it stamps "
        "`revoked_at`, and the hourly sweep is what deletes the row afterwards. So the three behaviour "
        "classes have nothing to assert - the row is deliberately still there once the delete has "
        "succeeded, which is the opposite of what `TestTheRowIsActuallyRemoved` requires, and a second "
        "revoke is a success rather than the 404 `TestASecondDeleteIsA404` expects. Note this reason is "
        "*not* `AuthenticationRequest`'s: that one turns on 'nothing offers a delete of it', and this "
        "resource offers exactly such a delete."
    ),
    "WebauthnCredential": (
        "A passkey, removed through the account's own credential routes together with the "
        "last-credential guard gating them."
    ),
}


def divers_own_hard_deleted() -> set[type[Base]]:
    """The hard-deleting resources a diver deletes one of, by uuid, through a route.

    Derived rather than listed: what is written down is only which of the addressable
    models are *not* one of these, so a model arriving in neither has to be classified
    before anything passes. `test_hard_delete.py` is where that fails.
    """
    return {model for model in addressable_hard_deleted() if model.__name__ not in NOT_A_DIVERS_OWN_RESOURCE}
