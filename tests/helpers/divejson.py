"""Conformance checking for DiveJSON documents, for the export tests.

**Conformance is the schema plus the rules the schema cannot express.** Spec §3 names five
classes of them - identifier uniqueness and referential closure, cross-member arithmetic,
profile-series integrity, the offset on `exported_at`, and the `format`/`version` member
order - and says the reference validator (`divejson validate`) checks both halves. A
document the vendored schema accepts can still be non-conforming, so a test that ran only
the schema would be asserting the weaker of the two claims.

This module is a **port of that validator**, kept deliberately close to it so that
`assert_conforms` here means what `divejson validate` means upstream. It is not a second
opinion about the format: where the two disagree, upstream is right and this is a bug. The
vendored schema and this port travel together - `tests/fixtures/divejson/README.md`
records which upstream commit they came from.

Only what a *writer* can violate is checked. The behavioural requirements addressed to
implementations - nothing invented (§5.4), unknown-member tolerance (§5.6), offset
preservation (§5.2) - are not properties of a document and are pinned by the writer's own
tests instead.
"""

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import best_match

# The parse half comes from the app rather than being restated here, and it is the one
# piece of this port that does: the logbook importer has to reject a duplicate member on
# the request path (spec §9), and two spellings of that rule is the two-shapes-for-one-fact
# problem the format's own supersession decision rejects. Re-exported so this module's
# public surface is unchanged - `parse_document` and `DuplicateMemberError` are still
# imported from here by `test_export_json.py`.
from src.app.services.logbook_import.reader import DuplicateMemberError, parse_document

__all__ = [
    "SCHEMA_PATH",
    "DuplicateMemberError",
    "assert_conforms",
    "conformance_issues",
    "parse_document",
]

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "divejson" / "divejson.schema.json"

# The collections whose records carry a `uuid` and can be referenced (spec §4).
_COLLECTIONS = (
    "dives",
    "trips",
    "courses",
    "sites",
    "species",
    "gear",
    "gear_sets",
    "gear_service_schedules",
    "gear_service_records",
    "certifications",
)

# `\Z`, not `$`: Python's `$` also matches just before a trailing newline, which would let
# `"…T08:00:00Z\n"` through the grammar check with the newline silently dropped.
_DATE_TIME = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(\.\d+)?([Zz]|[+-]\d{2}:\d{2})?\Z")


def conformance_issues(doc: Any) -> list[str]:
    """Every way `doc` fails DiveJSON 1.0. An empty list means conforming."""
    if not isinstance(doc, dict):
        return ["$: a DiveJSON document is a JSON object"]
    return [*_member_order_issues(doc), *_schema_issues(doc), *_semantic_issues(doc)]


def assert_conforms(doc: Any) -> None:
    issues = conformance_issues(doc)
    assert not issues, "document is not conforming DiveJSON:\n" + "\n".join(issues)


def _member_order_issues(doc: dict[str, Any]) -> list[str]:
    keys = list(doc)
    if not keys:
        return []
    if keys[0] != "format":
        return [f'$: the first member is "{keys[0]}"; "format" MUST come first (spec §4)']
    if len(keys) > 1 and keys[1] != "version":
        return [f'$: the second member is "{keys[1]}"; "version" MUST come second (spec §4)']
    return []


def _schema_issues(doc: dict[str, Any]) -> list[str]:
    with open(SCHEMA_PATH, encoding="utf-8") as handle:
        schema = json.load(handle)
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    issues = []
    for error in sorted(validator.iter_errors(doc), key=lambda e: list(e.absolute_path)):
        # A failure inside anyOf/if-then surfaces as a top-level error whose message dumps
        # the whole instance; `best_match` descends to the telling suberror.
        chosen = best_match([error]) or error
        issues.append(f"{'/'.join(str(part) for part in chosen.absolute_path) or '$'}: {chosen.message}")
    return issues


def _present(obj: dict[str, Any], member: str) -> bool:
    return obj.get(member) is not None


def _semantic_issues(doc: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    seen_uuids: dict[str, str] = {}

    _check_datetime(doc, "exported_at", "", issues, require_offset=True)

    diver = doc.get("diver")
    if isinstance(diver, dict):
        _claim_uuid(diver, "diver", seen_uuids, issues)
        _check_datetime(diver, "created_at", "diver", issues)

    collections = {name: [row for row in doc.get(name) or [] if isinstance(row, dict)] for name in _COLLECTIONS}
    for name, rows in collections.items():
        for index, row in enumerate(rows):
            here = f"{name}/{index}"
            _claim_uuid(row, here, seen_uuids, issues)
            _check_datetime(row, "created_at", here, issues)

    known = {
        name: {row["uuid"] for row in rows if isinstance(row.get("uuid"), str)} for name, rows in collections.items()
    }

    for index, dive in enumerate(collections["dives"]):
        _check_dive(dive, f"dives/{index}", known, seen_uuids, issues)

    for index, trip in enumerate(collections["trips"]):
        here = f"trips/{index}"
        _check_date_range(trip, here, issues)
        for loc_index, location in enumerate(trip.get("locations") or []):
            bbox = location.get("bbox")
            if isinstance(bbox, dict) and bbox.get("south", 0) > bbox.get("north", 0):
                issues.append(f"{here}/locations/{loc_index}/bbox: south exceeds north")

    for index, course in enumerate(collections["courses"]):
        _check_date_range(course, f"courses/{index}", issues)

    for index, gear_set in enumerate(collections["gear_sets"]):
        _check_reference_list(gear_set, "gear_uuids", known["gear"], "gear", f"gear_sets/{index}", issues)

    for index, schedule in enumerate(collections["gear_service_schedules"]):
        _check_reference(schedule, "gear_uuid", known["gear"], "gear", f"gear_service_schedules/{index}", issues)

    for index, record in enumerate(collections["gear_service_records"]):
        here = f"gear_service_records/{index}"
        _check_reference(record, "gear_uuid", known["gear"], "gear", here, issues)
        _check_reference(
            record,
            "gear_service_schedule_uuid",
            known["gear_service_schedules"],
            "gear_service_schedules",
            here,
            issues,
        )

    for index, certification in enumerate(collections["certifications"]):
        here = f"certifications/{index}"
        _check_reference(certification, "course_uuid", known["courses"], "courses", here, issues)
        for member in ("front_file", "back_file"):
            stored = certification.get(member)
            if isinstance(stored, dict):
                _claim_uuid(stored, f"{here}/{member}", seen_uuids, issues)

    for index, item in enumerate(collections["gear"]):
        _check_datetime(item, "archived_at", f"gear/{index}", issues)

    return issues


def _check_date_range(record: dict[str, Any], here: str, issues: list[str]) -> None:
    """`ends_on >= starts_on`, on a trip (§6.8) and on a course (§6.17)."""
    if _present(record, "starts_on") and _present(record, "ends_on") and record["ends_on"] < record["starts_on"]:
        issues.append(f"{here}: ends_on precedes starts_on")


def _check_dive(
    dive: dict[str, Any], here: str, known: dict[str, set[str]], seen_uuids: dict[str, str], issues: list[str]
) -> None:
    _check_datetime(dive, "started_at", here, issues)
    if _present(dive, "avg_depth") and _present(dive, "max_depth") and dive["avg_depth"] > dive["max_depth"]:
        issues.append(f"{here}: avg_depth exceeds max_depth")
    _check_reference(dive, "trip_uuid", known["trips"], "trips", here, issues)
    _check_reference(dive, "course_uuid", known["courses"], "courses", here, issues)
    _check_reference_list(dive, "site_uuids", known["sites"], "sites", here, issues)
    _check_reference_list(dive, "gear_uuids", known["gear"], "gear", here, issues)
    _check_reference_list(dive, "species_uuids", known["species"], "species", here, issues)

    for cyl_index, cylinder in enumerate(dive.get("cylinders") or []):
        cyl_path = f"{here}/cylinders/{cyl_index}"
        if _present(cylinder, "oxygen") and _present(cylinder, "helium"):
            if cylinder["oxygen"] + cylinder["helium"] > 100:
                issues.append(f"{cyl_path}: oxygen + helium exceeds 100 percent")
        if _present(cylinder, "start_pressure") and _present(cylinder, "end_pressure"):
            if cylinder["end_pressure"] > cylinder["start_pressure"]:
                issues.append(f"{cyl_path}: end_pressure exceeds start_pressure")

    source_file = dive.get("source_file")
    if isinstance(source_file, dict):
        _claim_uuid(source_file, f"{here}/source_file", seen_uuids, issues)

    profile = dive.get("profile")
    if isinstance(profile, dict):
        _check_profile(profile, here, issues)


def _check_profile(profile: dict[str, Any], here: str, issues: list[str]) -> None:
    latest = 0
    for channel in ("depth", "ceiling", "temperature"):
        series = profile.get(channel)
        if isinstance(series, dict):
            latest = max(latest, _check_series(series, f"{here}/profile/{channel}", issues))
    for series_index, series in enumerate(profile.get("pressures") or []):
        latest = max(latest, _check_series(series, f"{here}/profile/pressures/{series_index}", issues))
    # Events are deliberately **not** folded into `latest`. `duration` spans the samples,
    # and an event after the last one is conforming (spec §6.4): a marker pressed at the
    # surface after the recorder's final sample is real logbook data, and requiring
    # `duration` to swallow it would make a writer invent a sample span the file never
    # had - which is exactly what `_rebase_events` in `services/dive_profiles.py` refuses
    # to do at the other end.
    duration = profile.get("duration")
    if isinstance(duration, int) and duration < latest:
        issues.append(f"{here}/profile/duration: duration {duration} does not cover the latest sample at {latest}")


def _claim_uuid(obj: dict[str, Any], path: str, seen: dict[str, str], issues: list[str]) -> None:
    value = obj.get("uuid")
    if not isinstance(value, str):
        return
    if value in seen:
        issues.append(f"{path}: uuid {value} already used at {seen[value]}")
    else:
        seen[value] = path


def _check_reference(
    obj: dict[str, Any], member: str, targets: set[str], collection: str, path: str, issues: list[str]
) -> None:
    value = obj.get(member)
    if isinstance(value, str) and value not in targets:
        issues.append(f"{path}/{member}: references {value}, not present in {collection}")


def _check_reference_list(
    obj: dict[str, Any], member: str, targets: set[str], collection: str, path: str, issues: list[str]
) -> None:
    for index, value in enumerate(obj.get(member) or []):
        if isinstance(value, str) and value not in targets:
            issues.append(f"{path}/{member}/{index}: references {value}, not present in {collection}")


def _check_series(series: dict[str, Any], path: str, issues: list[str]) -> int:
    """Check one channel; returns the latest sample time seen (0 if none)."""
    times, values = series.get("times"), series.get("values")
    if not (isinstance(times, list) and isinstance(values, list)):
        return 0
    if len(times) != len(values):
        issues.append(f"{path}: times has {len(times)} samples but values has {len(values)}")
    if any(later <= earlier for earlier, later in zip(times, times[1:], strict=False)):
        issues.append(f"{path}: times is not strictly increasing")
    return max(times, default=0)


def _check_datetime(
    obj: dict[str, Any], member: str, path: str, issues: list[str], require_offset: bool = False
) -> None:
    value = obj.get(member)
    if not isinstance(value, str):
        return
    where = f"{path}/{member}" if path else member
    match = _DATE_TIME.match(value)
    if not match:
        issues.append(f"{where}: {value!r} is not a DiveJSON date-time")
        return
    base, fraction, offset = match.groups()
    # Normalize before the calendar check: `fromisoformat` is case-sensitive about `Z`,
    # which the format's grammar is not.
    normalized = base + ("." + (fraction[1:] + "000000")[:6] if fraction else "")
    if offset:
        normalized += "+00:00" if offset in ("Z", "z") else offset
    try:
        datetime.fromisoformat(normalized)
    except ValueError:
        issues.append(f"{where}: {value!r} is not a real calendar date-time")
        return
    if require_offset and offset is None:
        issues.append(f"{where}: must carry a UTC offset - it is generated, not recorded history (spec §5.2)")
