"""Logbook import: reading any number of files back into an account, whoever wrote them.

The mirror of `services/export`, and deliberately the same shape as its writer half - one
module that knows about the containers, one that knows what the records mean, one that
touches the database - with a fourth that runs an import of several files as those files
imported one at a time. The route layer sees a handful of functions and no internals.

Four stages, in this order, and the order is the design:

1. `parts.read_import_request` streams the request's `file` parts onto spools, after the
   caller is authenticated, under one bound on the memory they hold.
2. `reader.load_import` classifies every file by its bytes, opens zips, converts each file
   that is not DiveJSON already on its own, and parses each into an `ImportDocument`.
3. `species.resolve_catalog_gaps` fills the shared catalog from WoRMS - before anything
   is written, because it commits and can take a minute.
4. `batch.import_batch` plans each file (`planner.plan_import`) and writes it
   (`writer.write_import`) before the next is planned, in one transaction; the preview
   rolls it back, and the apply's caller commits, once.
"""

from .batch import BatchReport, batch_species, import_batch
from .parts import MAX_PARTS, ImportPart, ImportRequest, MalformedRequestError, batch_digest, read_import_request
from .planner import ImportPlan, plan_import, unresolved_aphia_ids
from .reader import (
    MAX_ARCHIVE_MEMBERS,
    MAX_ARCHIVE_SIZE,
    MAX_DOCUMENT_SIZE,
    DuplicateMemberError,
    ImportTooLargeError,
    LoadedBatch,
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
    "MAX_PARTS",
    "BatchReport",
    "DuplicateMemberError",
    "ImportPart",
    "ImportPlan",
    "ImportRequest",
    "ImportTooLargeError",
    "LoadedBatch",
    "LoadedImport",
    "MalformedImportError",
    "MalformedRequestError",
    "UnsupportedImportError",
    "batch_digest",
    "batch_species",
    "conversion_report",
    "formats_this_build_reads",
    "import_batch",
    "load_import",
    "parse_document",
    "plan_import",
    "read_import_request",
    "resolve_catalog_gaps",
    "unresolved_aphia_ids",
    "write_import",
]
