# DiveJSON fixtures

The JSON Schema every generated DiveJSON document is validated against, vendored the way
`../uddf/uddf_3.2.2.xsd` is and for the same reason: the export tests must be able to say a document
conforms without reaching the network.

## `divejson.schema.json` — the schema

- Source: <https://github.com/divejson/divejson/blob/main/schema/1.0/divejson.schema.json>,
  published at <https://divejson.org/schema/1.0/divejson.schema.json>
- Fetched: 2026-09-04, at `f8fb353` (the commit that last touched the schema; the repository was at
  `c5a4ee4`)
- Version: 1.0 — a working draft. The specification freezes at 1.0 once the reference
  implementation's export/import round-trip passes against it, and until then normative text, this
  schema and the upstream fixtures may change together without a version bump.
- License: MIT (the spec prose is CC BY 4.0; schema, fixtures and tools are MIT).

The file is unmodified. If it is ever re-fetched, update the commit and date above and re-run
`tests/test_export_json.py`.

## The schema is not the whole of conformance

Spec §3 lists five classes of normative requirement the JSON Schema cannot express — identifier
uniqueness and referential closure, cross-member arithmetic, profile-series integrity, the offset
requirement on `exported_at`, and the `format`/`version` member order. **A document this schema
accepts can still be non-conforming**, which is not a hypothetical: the writer's profile `duration`
was schema-valid and rule-invalid while the spec said `duration` had to cover the events too, and
nothing but a reading of §3 would have caught it.

`tests/helpers/divejson.py` is those rules, ported from the reference validator
(`divejson/validate.py` upstream) so that `assert_conforms` means the same thing here as
`divejson validate` does there. Port it forward when the upstream validator changes; the pairing is
the point, and a check that exists only here is a check the format does not actually make.

The port was checked against the upstream conformance corpus when it was written — all 3 valid
fixtures accepted and all 26 invalid ones rejected, with no disagreement — and the generated
documents were run through the real `divejson validate` as well. Neither check can live in this
suite: the fixtures and the CLI are in the other repository, and vendoring a copy of them would just
be a second thing to keep in step. Re-run both by hand against a checkout of `divejson/divejson`
when this file or that validator changes.
