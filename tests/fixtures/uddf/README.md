# UDDF 3.2.2 XML Schema

`uddf_3.2.2.xsd` is the official Universal Dive Data Format schema, vendored verbatim for
the export tests (`tests/test_export_uddf.py`), which validate every generated document
against it.

- Source: <https://www.streit.cc/resources/UDDF/v3.2.3/schema/uddf_3.2.2.xsd> (linked from
  chapter 11 of the UDDF documentation, <https://www.streit.cc/resources/UDDF/v3.2.3/en/schema.html>)
- Fetched: 2026-08-14
- Version: 3.2.2 — the newest published schema; the v3.2.3 documentation still links 3.2.2
  as its current XSD.
- License: the UDDF documentation this schema ships with is published under the GNU Free
  Documentation License ("UDDF is freely distributed" — see
  <https://www.streit.cc/resources/UDDF/v3.2.3/en/introduction.html>), which permits
  verbatim redistribution. Copyright © 2005–2018 Kai Schröder, Steffen Reith.

The file is unmodified. If it is ever re-fetched, update the date above and re-run the
export test suite — element order in `informationbeforedive` and `waypoint` is
sequence-sensitive and the writer in `src/app/services/export/uddf.py` is built against
exactly this revision.
