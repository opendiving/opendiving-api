"""Account field permissions shared by tool input schemas and output projections."""

from dataclasses import dataclass


@dataclass(frozen=True)
class AccountView:
    read_fields: frozenset[str]
    write_fields: frozenset[str]


PREFERENCE_FIELDS = frozenset(
    {
        "gear_service_emails",
        "renewal_reminder_emails",
        "year_in_review_emails",
        "units",
        "dive_form_hidden_fields",
        "dive_form_preset_uuid",
    }
)
PREFERENCES = AccountView(read_fields=PREFERENCE_FIELDS, write_fields=PREFERENCE_FIELDS)
PROFILE = AccountView(
    read_fields=frozenset(
        {
            "uuid",
            "name",
            "username",
            "email",
            "avatar_sha256",
            "avatar_original_sha256",
            "avatar_crop",
            "portrait_sha256",
            "portrait_original_sha256",
            "portrait_crop",
            "is_superuser",
        }
    ),
    write_fields=frozenset({"name", "username"}),
)
