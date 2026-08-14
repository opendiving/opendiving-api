# Export golden files

`dives.csv` is the exact bytes `services/export/tabular.py::write_dives_csv` produces for
`tests/helpers/export.py::full_bundle`, and `test_export_tabular.py` compares against it
byte for byte.

It exists because `dives.csv` is a **file people open in a spreadsheet**. A reordered
column, a changed quoting rule or a stray `None` in a cell breaks something no
field-by-field assertion covers, and the readable diff of a golden file is how you find
out you did it.

The line endings are CRLF (RFC 4180, and `csv.writer`'s default) and the file starts with
a UTF-8 byte-order mark, so both are part of what is being pinned. Do not let an editor
normalize either — `.gitattributes` marks this path `-text` to keep git out of it too.
`-text`, not `binary`: the latter also implies `-diff`, and a golden file whose diff you
cannot read is not doing its job.

Regenerate **deliberately**, after reading the diff and agreeing with it:

```bash
ENVIRONMENT=local SECRET_KEY=testsecret uv run python -c "
import sys; sys.path.insert(0, '.')
from pathlib import Path
from src.app.services.export.tabular import write_dives_csv
from tests.helpers.export import full_bundle
Path('tests/fixtures/export/dives.csv').write_text(''.join(write_dives_csv(full_bundle())), encoding='utf-8', newline='')
"
```
