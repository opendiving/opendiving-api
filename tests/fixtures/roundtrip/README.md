# Round-trip captures

`subsurface.ssrf` is Subsurface 6.0.5576's native save file after it imported
`tests/fixtures/uddf/demo-account.uddf` on 2026-08-14 — everything its data model kept. Every value
in it derives from seeded demo data.

It is here for the planned **Subsurface XML importer**, where its being a real file whose correct
answer is already known is the whole point: every field can be checked against the UDDF that
produced it. Nothing asserts against it today.

What Subsurface and divelogs.de each dropped, mangled or invented is written up in `DECISIONS.md` —
*"What Subsurface does with our UDDF, and what its own file does not"*, *"What divelogs.de does with
our UDDF"* and *"Trips and gear cannot survive a UDDF round-trip"*.

## The two UDDF re-exports are deliberately not here

Both programs' own UDDF exports were captured during that round-trip and then dropped: 200 KB
between them, and the interesting part of each is a handful of structural facts, all recorded in the
sections above — Subsurface's fails the 3.2.2 XSD 48 times (empty `<latitude/>`, ids that are not
NCNames, one beginning with a space, a duplicated `xs:ID`, an empty `<divetrip/>`), and
divelogs.de's carries no XML namespace at all, writes `<inifinity/>` for an infinite surface
interval, puts `<tankdata>` ahead of `<informationbeforedive>`, fabricates `0.000000` coordinates
and blanks any string containing an `&`.

The rest of each file was a re-rendering of a profile shape this repo no longer emits: both were
captured **before** the fix in *"Every UDDF waypoint carries a depth"*, so their waypoints are the
sawtooth and the discarded-temperature artefacts of an input no diver will ever hand them again. As
importer corpora they would teach the wrong lesson, and regenerating them from the current export is
a ten-minute round-trip that yields better samples. `subsurface.ssrf` survives the cut because it is
a *different format* — the one a planned parser has to read — not another rendering of the same
document.
