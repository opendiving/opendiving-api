from typing import Annotated, Literal

from pydantic import BaseModel, Field


class GeocodeResult(BaseModel):
    """One place, normalized away from whichever provider answered.

    The shape is deliberately provider-neutral: a search is answered by Photon and a pin by
    Nominatim (see `services.geocoding_service`), and the web and iOS clients read both the
    same way.

    `location` and `display_name` are both here because they answer different questions,
    and both are stored now. `location` is short and becomes a place's `name` - divers write
    "Ko Tao, Thailand", not a five-part address. A search result's is the place's own name
    and its country; a pin's is the settlement it falls in and its country. `display_name`
    is the full label, which becomes `full_name` and is what makes two otherwise identical
    entries in a search picker distinguishable: the place's name and every address part above
    it for a search result, Nominatim's own label for a pin. Neither member is renamed by
    that: these are the geocoder's own wire names, and the place object's are
    `schemas.location`'s.
    """

    latitude: Annotated[float, Field(ge=-90, le=90, examples=[28.5717])]
    longitude: Annotated[float, Field(ge=-180, le=180, examples=[34.5372])]
    # Every string below is bounded, because every one of them is written by a third party,
    # cached for a month and handed to every client. The provider is trusted to be honest,
    # not to be terse. `location`'s bound is the width of a place's `name` column, since
    # that is where it is headed; the others are simply sane ceilings. `services.geocoding_service`
    # truncates to these rather than letting an over-long value raise inside the normalizer.
    location: Annotated[str, Field(max_length=255, examples=["Dahab, Egypt"])]
    display_name: Annotated[str, Field(max_length=512, examples=["Blue Hole, Dahab, South Sinai, Egypt"])]
    # The place's own name, where it has one. Absent for a result that is only an address.
    name: Annotated[str | None, Field(max_length=255, default=None)]
    # Carried per-result rather than in an envelope: attribution is a licence condition of
    # the data itself, so it travels with the row it describes and survives a provider
    # swap (it is the provider's own `licence` string when it sends one). The client is
    # expected to render it wherever it shows these results.
    #
    # A wire format, not display copy. A credit ending in a bare URL is folded into the one
    # markdown shape the clients can parse - `[text](url)`, and nothing else - so the licence
    # can be *reached* rather than merely named; anything unfoldable is passed through and
    # rendered as plain text. See `services.geocoding_service._linked_attribution`, and
    # `DECISIONS.md` for why changing this shape is an API change and which client change has
    # to land first.
    attribution: Annotated[
        str,
        Field(
            max_length=255,
            examples=["[Data © OpenStreetMap contributors, ODbL 1.0.](https://osm.org/copyright)"],
        ),
    ]
    # Where a search result sits, for a picker to tell two same-named places apart: the same
    # role under the same names as on `DiveSiteSuggestion`, though not the same vocabulary -
    # these come from OpenStreetMap, the catalog's from Natural Earth. `region` is the finer
    # of the two. Each is null on every reverse answer, and wherever OSM records none.
    country: Annotated[str | None, Field(default=None, max_length=255, examples=["Egypt"])]
    region: Annotated[str | None, Field(default=None, max_length=255, examples=["South Sinai Governorate"])]
    # The OSM object a search result is, spelled as the dive-site catalog spells it, so a
    # client can drop a geocoder row that repeats a catalog row by comparing both fields.
    # Null on every reverse answer.
    source: Annotated[Literal["osm"] | None, Field(default=None, examples=["osm"])]
    source_id: Annotated[str | None, Field(default=None, max_length=64, examples=["node/27043265"])]
    # The place's extent, when the provider sends one. Four named floats rather than a
    # nested object or a list, because that is how a place stores them
    # (`schemas.location.LocationInput`) and a client that picks a result writes it
    # straight back - a shape change in between would be a mapping nobody needs.
    #
    # All four or none of them: a partial box is not a box. They are absent for a result
    # the provider sent no box for, for one whose box did not parse, and for every
    # reverse lookup - a pin's answer is a name for a position the caller already has,
    # and framing a map around a country is a search-side concern. West > east is legal
    # and means the box crosses the antimeridian.
    bbox_south: Annotated[float | None, Field(default=None, ge=-90, le=90, examples=[9.89])]
    bbox_north: Annotated[float | None, Field(default=None, ge=-90, le=90, examples=[9.98])]
    bbox_west: Annotated[float | None, Field(default=None, ge=-180, le=180, examples=[123.35])]
    bbox_east: Annotated[float | None, Field(default=None, ge=-180, le=180, examples=[123.44])]
