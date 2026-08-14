# Round-trip captures

What two real logbook programs made of `tests/fixtures/uddf/demo-account.uddf` when it was imported
on 2026-08-14. They are the de-facto conformance tests for the UDDF writer — a schema says a
document is well-formed, these say whether a diver can actually leave with their data — and they are
the corpus the planned Subsurface/UDDF *importer* will be developed against, where the right answer
is already known.

| File              | What it is                                                             |
| ----------------- | ---------------------------------------------------------------------- |
| `subsurface.ssrf` | Subsurface 6.0.5576's native save file: everything its data model kept |
| `subsurface.uddf` | what its UDDF exporter then wrote back out, which is strictly less     |
| `divelogs.uddf`   | divelogs.de's (divelogs.org) UDDF export after importing the same file |

Everything in them derives from seeded demo data. The one identifier that is not is
`<owner id="aleskiontherun"/>` in `divelogs.uddf` — the public divelogs.org handle of the account
the corpus was imported into, kept because the file is a verbatim capture.

**They were captured before the fix they caused**, and that is the point of keeping them. Both files
show the failure described in `DECISIONS.md` under *"Every UDDF waypoint carries a depth, because
the alternative broke both importers"*: the export then emitted waypoints at the union of every
channel's timestamps, so a temperature sample between two depth samples became a waypoint with no
`<depth>`. Subsurface dropped those waypoints (706 temperature samples arrived as 29); divelogs.de
read the missing depth as **zero**, and its stored profile — visible in its own UI, not just in this
export — saws between the real depth and the surface on every other sample. `demo-account.uddf` has
since been regenerated from the fixed writer and no longer contains a depth-less waypoint, so
re-running either import today would produce different files. Nothing asserts against these
captures; they are evidence and input, not test data.

Two further traps for whoever writes the importer, both recorded here because they are invisible
until you try:

- **Neither file validates.** `subsurface.uddf` fails the vendored UDDF 3.2.2 XSD 48 times — empty
  `<latitude/>`/`<longitude/>`, ids that are not NCNames (`mix(21/0)`, `2bbb3390`, and one that
  begins with a space), the same id on a `<repetitiongroup>` and its `<dive>`, an empty
  `<divetrip/>`. `divelogs.uddf` carries **no XML namespace at all**, so it is not UDDF to any
  validator, plus `<inifinity/>` (sic) and `<tankdata>` ahead of `<informationbeforedive>`. Schema
  validation cannot be the importer's front door.
- **An export is not a data model.** Both programs kept things their own exporters then threw away —
  water temperature in Subsurface's case, every string containing an `&` in divelogs.de's. Reading
  only the re-export libels the importer; the write-up in `DECISIONS.md` is based on the `.ssrf` and
  on the two web pages that settled the rest.
