from datetime import datetime
from typing import Annotated, Any, ClassVar

from divejson.validate import WHITE_SPACE
from pydantic import AfterValidator, BaseModel, BeforeValidator, ConfigDict, Field, StringConstraints

from ..core.schemas import PublicUUIDSchema, RejectsExplicitNulls

# DiveJSON §6.2's bound on a tag, and the width of the column behind it.
TAG_NAME_MAX = 64

TAG_NOT_FOUND = "Tag not found."


def trim_tag(value: Any) -> Any:
    """Strip Unicode White_Space from both ends - DiveJSON §3 rule 8's trim, which is not
    `str.strip()`: that also strips U+001C to U+001F."""
    return value.strip(WHITE_SPACE) if isinstance(value, str) else value


def tag_key(name: str) -> str:
    """The comparison rule 8 makes, in Python: trimmed and Unicode full case-folded.

    `ux_tag_user_id_name_folded` is the authority - Postgres's `casefold()` under
    `pg_unicode_fast` - and this agrees with it on everything either is likely to meet.
    """
    return name.strip(WHITE_SPACE).casefold()


# Stored trimmed, so the folded unique index is the trimmed key.
TagName = Annotated[
    str,
    StringConstraints(min_length=1, max_length=TAG_NAME_MAX),
    BeforeValidator(trim_tag),
    Field(examples=["night"]),
]


def _first_of_each_fold(names: list[str] | None) -> list[str] | None:
    """Collapse names rule 8 calls one tag, keeping the first spelling and its position."""
    if names is None:
        return None
    kept: dict[str, str] = {}
    for name in names:
        kept.setdefault(tag_key(name), name)
    return list(kept.values())


TagsWrite = Annotated[
    list[TagName],
    AfterValidator(_first_of_each_fold),
    Field(
        default_factory=list,
        description="The dive's tags by name, in the diver's order. Each is trimmed and matched to the diver's tag "
        "of that name compared case-folded, or creates one; two that fold to one keep the first spelling.",
    ),
]
TagsUpdate = Annotated[
    list[TagName] | None,
    AfterValidator(_first_of_each_fold),
    Field(default=None, description="Replaces the tags wholesale, by name. Omit to leave them."),
]


class TagRead(PublicUUIDSchema):
    """A tag as `GET /tags` serves it. `dive_count` is the live dives carrying it, and may be
    zero: a tag stays until the diver deletes it."""

    name: str
    dive_count: Annotated[int, Field(description="Live dives carrying this tag")]
    created_at: datetime
    updated_at: datetime | None = None


class TagReadInternal(PublicUUIDSchema):
    """Mirrors the `tag` table's columns, for server-side lookups only."""

    id: int
    user_id: int
    name: str
    created_at: datetime
    updated_at: datetime | None = None


class TagCreateInternal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: int
    name: str


class TagUpdate(RejectsExplicitNulls):
    """Request body for `PATCH /tag/{uuid}`: a rename, the only thing a tag has to change."""

    model_config = ConfigDict(extra="forbid")

    NON_NULLABLE_FIELDS: ClassVar[tuple[str, ...]] = ("name",)

    name: Annotated[
        str | None,
        StringConstraints(min_length=1, max_length=TAG_NAME_MAX),
        BeforeValidator(trim_tag),
        Field(default=None, examples=["night dive"]),
    ]


class TagUpdateInternal(TagUpdate):
    updated_at: datetime
