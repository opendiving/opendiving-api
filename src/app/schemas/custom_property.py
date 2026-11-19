"""Custom properties: the diver's own key-value pairs on a record, for what no column holds.

A dive computer's firmware, a regulator's service shop, a trip's booking reference - facts a
diver wants beside a record that the model has no member for. Each is a text key and a text
value, kept in the order the diver wrote them, on the records a diver owns: dives, dive sites,
trips, courses, gear items, gear sets, people, contacts and certifications.

On the wire a record carries `custom_properties`, a list of `{key, value}` objects, and a write
replaces the list whole, as a contact's `roles` are replaced. A list rather than an object so
the diver's order survives, which a JSON object's member order is not promised to.
"""

from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from ..core.schemas import Trimmed

CUSTOM_PROPERTY_KEY_MAX = 100
CUSTOM_PROPERTY_VALUE_MAX = 1000
# Far past what a diver types by hand, and a bound on the payload a record carries in every
# list and export row - not a product rule.
CUSTOM_PROPERTIES_MAX = 50

DUPLICATE_KEY_MESSAGE = "custom_properties holds the key {key!r} twice; keys are unique ignoring case"


class CustomProperty(BaseModel):
    """One property on the way in: both members trimmed and non-empty."""

    model_config = ConfigDict(extra="forbid")

    key: Annotated[
        str,
        Trimmed,
        Field(
            max_length=CUSTOM_PROPERTY_KEY_MAX,
            examples=["Firmware"],
            description="What the value is, unique on the record ignoring case",
        ),
    ]
    value: Annotated[
        str,
        Trimmed,
        Field(max_length=CUSTOM_PROPERTY_VALUE_MAX, examples=["2.4.1"], description="The value, as text"),
    ]


class CustomPropertyRead(BaseModel):
    """One property as stored. Plain strings, as `StoredName` is: a read takes the row as it is."""

    key: str
    value: str


def unique_keys(properties: list[CustomProperty] | None) -> list[CustomProperty] | None:
    """Refuse a list naming one key twice, compared case-folded as tag names are.

    Refused rather than folded: unlike a duplicate role, two values under one key are two
    things the diver said, and keeping either would silently drop the other.
    """
    if properties is None:
        return None
    seen: set[str] = set()
    for prop in properties:
        folded = prop.key.casefold()
        if folded in seen:
            raise ValueError(DUPLICATE_KEY_MESSAGE.format(key=prop.key))
        seen.add(folded)
    return properties


def merge_custom_properties(held: list[dict[str, str]], joining: list[dict[str, str]]) -> list[dict[str, str]]:
    """`held` with `joining`'s properties after it, as two lists one record now carries.

    For two records becoming one - a diver's site replaced with a Commons record they already had
    properties on, two dives merged: a key both name keeps its place in `held`, and two different
    values under it are joined as two notes are, so neither is lost. The result may pass
    `CUSTOM_PROPERTIES_MAX` or a value's bound; a read takes it as it is, and the diver trims it on
    their next write.
    """
    merged = [dict(prop) for prop in held]
    by_key = {prop["key"].casefold(): prop for prop in merged}
    for prop in joining:
        kept = by_key.get(prop["key"].casefold())
        if kept is None:
            merged.append(dict(prop))
            by_key[prop["key"].casefold()] = merged[-1]
        elif kept["value"] != prop["value"]:
            kept["value"] = f"{kept['value']}; {prop['value']}"
    return merged


_DESCRIPTION = (
    "The diver's own key-value pairs, in the diver's order. Keys are unique ignoring case. "
    "A write replaces the whole list"
)
_EXAMPLES = [[{"key": "Firmware", "value": "2.4.1"}]]


class CustomPropertiesWrite(BaseModel):
    """`custom_properties` on a create body: optional, an empty list when omitted."""

    custom_properties: Annotated[
        list[CustomProperty],
        Field(default_factory=list, max_length=CUSTOM_PROPERTIES_MAX, description=_DESCRIPTION, examples=_EXAMPLES),
        AfterValidator(unique_keys),
    ]


class CustomPropertiesUpdate(BaseModel):
    """`custom_properties` on a PATCH body: omitted leaves the list, `[]` clears it.

    Every update schema mixing this in names `custom_properties` in its `NON_NULLABLE_FIELDS`,
    since the column is `NOT NULL` and an empty list is how "none" is spelled.
    """

    custom_properties: Annotated[
        list[CustomProperty] | None,
        Field(
            default=None,
            max_length=CUSTOM_PROPERTIES_MAX,
            description=f"{_DESCRIPTION}; an empty list clears it, null is refused",
            examples=_EXAMPLES,
        ),
        AfterValidator(unique_keys),
    ]


class CustomPropertiesRead(BaseModel):
    """`custom_properties` on a read: always present, `[]` when the record has none."""

    custom_properties: Annotated[list[CustomPropertyRead], Field(default_factory=list, description=_DESCRIPTION)]
