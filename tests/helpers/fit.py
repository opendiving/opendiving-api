"""A minimal FIT *writer*, so FIT fixtures can be declared inline like every other
parser test's.

FIT is the one supported export that is binary, which would otherwise force the choice
between committing opaque `.fit` blobs and not testing the parser's interesting cases at
all. Neither is good: a blob can't be edited to express "a file whose developer field
shadows a native one" or "a dive with no `activity` message", which is exactly what
`FitParser` needs pinned down.

So this encodes messages the way a dive computer does, driven by `fitdecode`'s own copy
of the global FIT profile - field numbers, base types, scale factors and enum values are
all looked up rather than hardcoded, so a fixture says `session(max_depth=32.41)` and
cannot drift out of step with the profile the parser reads through.

Only what these tests need is implemented: little-endian, one definition per local
message type, no compressed timestamp headers, no accumulators.
"""

import struct
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from fitdecode.profile import BASE_TYPES, MESSAGE_TYPES
from fitdecode.types import FieldType

# FIT timestamps count seconds from this epoch, not the Unix one.
_FIT_EPOCH = datetime(1989, 12, 31, tzinfo=UTC)

_STRING = 0x07
_BYTE = 0x0D
FLOAT32 = 0x88
UINT8 = 0x02

# Record-header bits (the "normal" header form - bit 7 clear).
_DEFINITION_MESSAGE = 0x40
_DEVELOPER_DATA = 0x20

# The 16-entry nibble table the FIT spec defines its CRC-16 by.
_CRC_TABLE = (
    0x0000, 0xCC01, 0xD801, 0x1400, 0xF001, 0x3C00, 0x2800, 0xE401,
    0xA001, 0x6C00, 0x7800, 0xB401, 0x5000, 0x9C01, 0x8801, 0x4400,
)  # fmt: skip

# Developer data is announced once per file under this index, which every
# `field_description` and every developer field then refers back to.
_DEV_DATA_INDEX = 0


def _crc16(data: bytes) -> int:
    crc = 0
    for byte in data:
        for nibble in (byte & 0x0F, (byte >> 4) & 0x0F):
            table = _CRC_TABLE[crc & 0x0F]
            crc = (crc >> 4) & 0x0FFF
            crc = crc ^ table ^ _CRC_TABLE[nibble]
    return crc


@dataclass(frozen=True, slots=True)
class DevField:
    """A developer field: one the file defines for itself via `field_description`.

    Chiefly here to reproduce the collision Suunto's exporter creates, where a developer
    field carries the same *name* as a native profile field on the same message.
    """

    name: str
    value: Any
    base_type: int = FLOAT32
    field_number: int = 0
    units: str | None = None


@dataclass(frozen=True, slots=True)
class Message:
    """One data message, named as the FIT profile names it."""

    name: str
    values: dict[str, Any] = field(default_factory=dict)
    dev_fields: tuple[DevField, ...] = ()


def message(name: str, *dev_fields: DevField, **values: Any) -> Message:
    """Declare a message: `message("session", sport="diving", max_depth=32.41)`.

    Values are given in the units a human reads (meters, seconds, `"diving"`); the
    encoder applies the profile's scale factor and enum mapping.
    """
    return Message(name=name, values=values, dev_fields=dev_fields)


def _message_type(name: str) -> tuple[int, Any]:
    for global_number, message_type in MESSAGE_TYPES.items():
        if message_type.name == name:
            return global_number, message_type
    raise LookupError(f"No such FIT message in the profile: {name}")


def _field_def(message_type: Any, name: str) -> tuple[int, Any]:
    for number, profile_field in message_type.fields.items():
        if profile_field.name == name:
            return number, profile_field
    raise LookupError(f"No such field on FIT message {message_type.name}: {name}")


def _encode(base_type: int, raw: Any) -> bytes:
    """Pack an already-raw value in its base type's wire form."""
    if base_type == _STRING:
        return str(raw).encode() + b"\x00"
    if base_type == _BYTE and isinstance(raw, (bytes, bytearray)):
        return bytes(raw)
    return struct.pack("<" + BASE_TYPES[base_type].fmt, raw)


def _raw_value(profile_field: Any, value: Any) -> Any:
    """Turn a human-facing value into the integer the file stores.

    The three transformations the profile describes, in the order a decoder undoes them:
    a `date_time` counts from the FIT epoch, an enum field stores its numeric member, and
    a scaled field stores `(value + offset) * scale`.
    """
    field_type = profile_field.type
    if isinstance(value, datetime):
        return int((value - _FIT_EPOCH).total_seconds())
    if isinstance(field_type, FieldType) and field_type.enum and isinstance(value, str):
        for number, member in field_type.enum.items():
            if member == value:
                return number
        raise LookupError(f"No such member of FIT enum {field_type.name}: {value}")
    if profile_field.scale:
        return round((value + (profile_field.offset or 0)) * profile_field.scale)
    return value


def _base_type_of(profile_field: Any) -> int:
    field_type = profile_field.type
    base = field_type.base_type if isinstance(field_type, FieldType) else field_type
    return int(base.identifier)


def _definition(
    local_type: int,
    global_number: int,
    fields: list[tuple[int, int, int]],
    dev: list[tuple[int, int, int]],
) -> bytes:
    """Encode a definition message: what the next data message's bytes mean."""
    header = _DEFINITION_MESSAGE | (_DEVELOPER_DATA if dev else 0) | local_type
    out = bytearray([header, 0, 0])  # reserved, architecture (0 = little endian)
    out += struct.pack("<H", global_number)
    out.append(len(fields))
    for number, size, base_type in fields:
        out += bytes([number, size, base_type])
    if dev:
        out.append(len(dev))
        for number, size, index in dev:
            out += bytes([number, size, index])
    return bytes(out)


def _encode_message(msg: Message, local_type: int) -> bytes:
    """Encode one message as a definition record followed by its data record."""
    global_number, message_type = _message_type(msg.name)

    definitions: list[tuple[int, int, int]] = []
    payload = bytearray()
    for name, value in msg.values.items():
        number, profile_field = _field_def(message_type, name)
        base_type = _base_type_of(profile_field)
        encoded = _encode(base_type, _raw_value(profile_field, value))
        definitions.append((number, len(encoded), base_type))
        payload += encoded

    dev_definitions: list[tuple[int, int, int]] = []
    for dev_field in msg.dev_fields:
        encoded = _encode(dev_field.base_type, dev_field.value)
        dev_definitions.append((dev_field.field_number, len(encoded), _DEV_DATA_INDEX))
        payload += encoded

    record = _definition(local_type, global_number, definitions, dev_definitions)
    return record + bytes([local_type]) + bytes(payload)


def _dev_declarations(messages: tuple[Message, ...]) -> list[Message]:
    """The `developer_data_id` / `field_description` preamble a file needs before it may
    carry developer fields at all - emitted automatically so a fixture only has to say
    which developer fields it wants."""
    declared = [dev for msg in messages for dev in msg.dev_fields]
    if not declared:
        return []

    preamble = [
        message(
            "developer_data_id",
            application_id=b"OpenDivingTests\x00",
            developer_data_index=_DEV_DATA_INDEX,
        )
    ]
    seen: set[int] = set()
    for dev in declared:
        if dev.field_number in seen:
            continue
        seen.add(dev.field_number)
        preamble.append(
            message(
                "field_description",
                developer_data_index=_DEV_DATA_INDEX,
                field_definition_number=dev.field_number,
                fit_base_type_id=dev.base_type,
                field_name=dev.name,
                **({"units": dev.units} if dev.units is not None else {}),
            )
        )
    return preamble


def fit_file(*messages: Message) -> bytes:
    """Assemble messages into a complete, CRC-correct FIT file.

    A 12-byte header (the short form, which carries no CRC of its own) followed by the
    records and the file CRC over everything before it.
    """
    body = bytearray()
    for index, msg in enumerate(_dev_declarations(messages) + list(messages)):
        # A fresh local message type per message, cycling through the 16 available.
        # Simpler than tracking which definition is currently bound to which slot, and
        # indistinguishable to a reader.
        body += _encode_message(msg, index % 16)

    header = bytearray([12, 0x20])
    header += struct.pack("<H", 2140)  # profile version, cosmetic here
    header += struct.pack("<I", len(body))
    header += b".FIT"

    out = bytes(header) + bytes(body)
    return out + struct.pack("<H", _crc16(out))


def dive_fit_file(*messages: Message, sport: str = "diving", **session: Any) -> bytes:
    """The common shape: a `file_id`, one diving `session`, and whatever else is passed.

    `session` keywords are merged into the session message, so a test that cares about
    one field says so and inherits a plausible dive around it.
    """
    defaults: dict[str, Any] = {
        "start_time": datetime(2026, 4, 17, 9, 49, 23, tzinfo=UTC),
        "total_elapsed_time": 4301.72,
        "max_depth": 45.91,
        "avg_depth": 19.43,
    }
    return fit_file(
        message("file_id", type="activity", manufacturer="suunto"),
        *messages,
        message("session", sport=sport, **(defaults | session)),
    )
