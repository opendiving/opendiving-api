# Dive-computer file fixtures

Real inputs to the one reader, copied from the `divejson` package's own conformance corpus
(`divejson/divejson-py`, `fixtures/`, at the commit released as `divejson` 0.16.0) under its MIT
licence. Each keeps the package's file name; the package's `fixtures/README.md` says what each one
exercises there.

| File                                          | Recorded whole or built by hand                                              |
| --------------------------------------------- | ---------------------------------------------------------------------------- |
| `suunto-ocean-2026.fit`, `suunto-ocean.fit`   | Recorded whole: a Suunto Ocean's own FIT exports of two dives                |
| `suunto-ocean-2026.json`, `suunto-ocean.json` | Reduced from the Suunto app's exports of the same two dives                  |
| `suunto-d5.json`                              | Reduced from a Suunto D5 export: two gases, one transmitter                  |
| `nitrox-deco.xml`                             | Reduced from a Suunto DM5 export: two cylinders, a stated bottom temperature |
| `not-a-dive.json`                             | Constructed: a Suunto app export of an activity that is not a dive           |
| `two-computers.ssrf`                          | Reduced from a Subsurface save: one dive, two computers' records             |

The two `suunto-ocean-2026` files are one dive exported twice by one computer, and so are the two
`suunto-ocean` files: the pairs the door, label and fill tests run over. The second pair's JSON
records no gas mix, which is what makes its cylinders join the FIT's by position.

The bytes matter - a stored file is identified by its digest - so `.gitattributes` keeps git from
normalizing the text ones. Replace a file only from the package's corpus, and say which release it
came from here.
