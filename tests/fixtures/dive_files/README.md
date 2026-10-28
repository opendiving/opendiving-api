# Dive-computer file fixtures

Real inputs to the one reader, copied from the `divejson` package's own conformance corpus
(`divejson/divejson-py`, `fixtures/`, at the commit released as `divejson` 0.16.0, and
`suunto-ocean-2026.json` and the two `ocean-poor-first-fix` files at the one released as 0.21.0, and
the two `perdix3` files at the one released as 0.33.0, and the two `ocean-heart-rate` files at the
one released as 0.35.0) under its MIT licence. Each keeps the package's file name; the package's
`fixtures/README.md` says what each one exercises there.

| File                                          | Recorded whole or built by hand                                                                       |
| --------------------------------------------- | ----------------------------------------------------------------------------------------------------- |
| `suunto-ocean-2026.fit`, `suunto-ocean.fit`   | Recorded whole: a Suunto Ocean's own FIT exports of two dives                                         |
| `suunto-ocean-2026.json`, `suunto-ocean.json` | Reduced from the Suunto app's exports of the same two dives                                           |
| `ocean-poor-first-fix.fit`                    | Recorded whole: a Suunto Ocean's own FIT export of a third dive                                       |
| `ocean-poor-first-fix.json`                   | Reduced from the Suunto app's export of that dive: its first fix after surfacing states 47 m of error |
| `ocean-heart-rate.fit`                        | Recorded whole: a Suunto Ocean's own FIT export of a fourth dive, with its wrist heart rate           |
| `ocean-heart-rate.json`                       | Reduced from the Suunto app's export of that dive, its `HR` samples in hertz                          |
| `suunto-d5.json`                              | Reduced from a Suunto D5 export: two gases, one transmitter                                           |
| `nitrox-deco.xml`                             | Reduced from a Suunto DM5 export: two cylinders, a stated bottom temperature                          |
| `not-a-dive.json`                             | Constructed: a Suunto app export of an activity that is not a dive                                    |
| `two-computers.ssrf`                          | Reduced from a Subsurface save: one dive, two computers' records                                      |
| `perdix3-dives-1-and-8.db`                    | Reduced from a Shearwater Cloud Desktop database export: two of a Perdix 3's dives                    |
| `perdix3-dive-8.xml`                          | Reduced from Shearwater Cloud Desktop's XML export of the second of those dives                       |

The two `suunto-ocean-2026` files are one dive exported twice by one computer, and so are the two
`suunto-ocean` files: the pairs the door, label and fill tests run over. The second pair's JSON
records no gas mix, which is what makes its cylinders join the FIT's by position. The two
`ocean-poor-first-fix` files are a third pair, the one the exit tests run over: the JSON states each
fix's error, and the FIT none. The two `ocean-heart-rate` files are a fourth, the only pair carrying
a heart rate: the FIT's runs past the profile cap, which is what the average is tested against.

The bytes matter - a stored file is identified by its digest - so `.gitattributes` keeps git from
normalizing the text ones. Replace a file only from the package's corpus, and say which release it
came from here.
