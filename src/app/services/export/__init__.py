"""Full export of a diver's logbook, in three shapes.

- `envelope.py` writes **`logbook.divejson`**, a DiveJSON 1.0 document: the complete
  structured copy, including everything UDDF has no slot for - gear sets, service history,
  c-cards, training courses, per-cylinder role and usage, multi-site visit order - so
  nothing is reachable only through the lossy file. DiveJSON is the open interchange
  format this project maintains (<https://divejson.org>), and this is its reference
  writer, which is what makes the claim checkable rather than a slogan.
- `uddf.py` writes a **UDDF 3.2.2** document - the older interchange format Subsurface,
  divelogs.de and MacDive read. It is what makes "take my dives anywhere" true for the
  apps that predate DiveJSON.
- `tabular.py` writes the **CSV** set, headed by the flat `dives.csv` a diver opens in a
  spreadsheet.
- `archive.py` puts all of the above in one zip alongside every stored dive-computer
  export and c-card image.

All four read one `ExportBundle` (`loader.py`), which is the single batched query pass.

Two rules hold across the package and are worth stating once:

**Nothing is invented.** The same discipline as the parsers (*"Parsers report what a file
recorded"* in DECISIONS.md): a value the log does not hold is an absent element or an
empty cell - never a default wearing a reading's clothes. In DiveJSON that rule is
normative and absence is its *only* spelling, so the writer emits no nulls at all (spec
§5.4). Where a format *requires* something we do not have, that is called out at the site.

**Nothing is held whole.** Every writer is a generator, blobs are read one row at a time,
and the endpoints spool to disk past 32 MB. A thousand-dive logbook with its profiles is
hundreds of megabytes, and the export is exactly the request a diver makes once and a
crawler could make repeatedly.
"""

from .archive import spool, spool_text, write_archive
from .envelope import write_divejson
from .loader import ExportBundle, load_export_bundle
from .naming import export_filename, gas_name
from .tabular import write_dives_csv
from .uddf import write_uddf

__all__ = [
    "ExportBundle",
    "export_filename",
    "gas_name",
    "load_export_bundle",
    "spool",
    "spool_text",
    "write_archive",
    "write_divejson",
    "write_dives_csv",
    "write_uddf",
]
