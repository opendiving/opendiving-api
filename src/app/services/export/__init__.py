"""Full export of a diver's logbook, in three shapes.

- `uddf.py` writes a **UDDF 3.2.2** document - the interchange format Subsurface,
  divelogs.de and MacDive read. It is what makes "take my dives anywhere" checkable.
- `tabular.py` writes the **CSV** set, headed by the flat `dives.csv` a diver opens in a
  spreadsheet.
- `envelope.py` writes **`export.json`**, the complete structured copy, including
  everything UDDF has no slot for - gear sets, service history, c-cards, per-cylinder
  role, multi-site visit order - so nothing is reachable only through the lossy file.
- `archive.py` puts all of the above in one zip alongside every stored dive-computer
  export and c-card image.

All four read one `ExportBundle` (`loader.py`), which is the single batched query pass.

Two rules hold across the package and are worth stating once:

**Nothing is invented.** The same discipline as the parsers (*"Parsers report what a file
recorded"* in DECISIONS.md): a value the log does not hold is an absent element, an empty
cell or a JSON null - never a default wearing a reading's clothes. Where a format
*requires* something we do not have, that is called out at the site.

**Nothing is held whole.** Every writer is a generator, blobs are read one row at a time,
and the endpoints spool to disk past 32 MB. A thousand-dive logbook with its profiles is
hundreds of megabytes, and the export is exactly the request a diver makes once and a
crawler could make repeatedly.
"""

from .archive import spool, spool_text, write_archive
from .envelope import write_export_json
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
    "write_dives_csv",
    "write_export_json",
    "write_uddf",
]
