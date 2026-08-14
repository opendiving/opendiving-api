# Subsurface round-trip artifacts

What Subsurface 6.0.5576 (macOS, libdivecomputer 0.10.0) made of
`tests/fixtures/uddf/demo-account.uddf` when it was imported on 2026-08-14, captured two ways:

- **`roundtrip.ssrf`** — Subsurface's own save file, i.e. everything its data model kept.
- **`roundtrip.uddf`** — what its *UDDF exporter* then wrote back out, which is strictly less. The
  gap between the two files is the interesting part: water temperature, for one, survives the import
  and is then dropped on the way out, so the re-export alone would have libelled the importer.

Both are derived from seeded demo data, so there is nothing personal in either.

They are **inputs for the planned Subsurface/UDDF importer**, not test data — nothing asserts
against them today, deliberately. Their value is that the correct answer is already known: every
value in them can be checked against `demo-account.uddf`, which the importer will one day have to
reproduce. What each one drops or mangles is written up in `DECISIONS.md` under *"What Subsurface
does with our UDDF, and what its own file does not"*.

The one thing to know before opening `roundtrip.uddf`: it **fails the vendored UDDF 3.2.2 XSD 48
times** — empty `<latitude/>`/`<longitude/>`, ids that are not NCNames (`mix(21/0)`, `2bbb3390`, and
one that begins with a space), the same id on a `<repetitiongroup>` and its `<dive>`, and an empty
`<divetrip/>`. That is not a corrupted capture, it is what Subsurface writes, and it is the reason
the importer cannot use schema validation as its front door.
