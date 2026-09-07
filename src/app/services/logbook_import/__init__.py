"""Logbook import: reading a logbook back into an account, whoever wrote it.

The mirror of `services/export`, and deliberately the same shape as its writer half - one
module that knows about the container, one that knows what the records mean, one that
touches the database. The route layer sees three functions and no internals.

Four stages, in this order, and the order is the design:

1. `reader.load_import` spools the upload, converts it if it is not DiveJSON already,
   and parses it into an `ImportDocument`.
2. `species.resolve_catalog_gaps` fills the shared catalog from WoRMS - before anything
   is written, because it commits and can take a minute.
3. `planner.plan_import` decides what would happen. This alone is what preview runs.
4. `writer.write_import` issues the statements. The caller commits, once.
"""

from .planner import ImportPlan, plan_import, unresolved_aphia_ids
from .reader import (
    MAX_ARCHIVE_MEMBERS,
    MAX_ARCHIVE_SIZE,
    MAX_DOCUMENT_SIZE,
    DuplicateMemberError,
    ImportTooLargeError,
    LoadedImport,
    MalformedImportError,
    UnsupportedImportError,
    conversion_report,
    formats_this_build_reads,
    load_import,
    parse_document,
)
from .species import resolve_catalog_gaps
from .writer import write_import

__all__ = [
    "MAX_ARCHIVE_MEMBERS",
    "MAX_ARCHIVE_SIZE",
    "MAX_DOCUMENT_SIZE",
    "DuplicateMemberError",
    "ImportPlan",
    "ImportTooLargeError",
    "LoadedImport",
    "MalformedImportError",
    "UnsupportedImportError",
    "conversion_report",
    "formats_this_build_reads",
    "load_import",
    "parse_document",
    "plan_import",
    "resolve_catalog_gaps",
    "unresolved_aphia_ids",
    "write_import",
]
