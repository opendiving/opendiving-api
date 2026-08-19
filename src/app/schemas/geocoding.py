from typing import Annotated

from pydantic import BaseModel, Field


class GeocodeResult(BaseModel):
    """One place, normalized away from whichever provider answered.

    The shape is deliberately provider-neutral so `GEOCODER_URL` can point somewhere else
    without the web and iOS clients noticing (see `services.geocoding_service`).

    `location` and `display_name` are both here because they answer different questions.
    `location` is short, composed from the provider's structured address, and is what gets
    persisted onto `dive_site.location` - divers write "Dahab, Egypt", not a seven-part
    postal address. `display_name` is the provider's full label, which is what makes two
    otherwise identical entries in a search picker distinguishable.
    """

    latitude: Annotated[float, Field(ge=-90, le=90, examples=[28.5717])]
    longitude: Annotated[float, Field(ge=-180, le=180, examples=[34.5372])]
    # Every string below is bounded, because every one of them is written by a third party,
    # cached for a month and handed to every client. The provider is trusted to be honest,
    # not to be terse. `location`'s bound is the width of `dive_site.location`, since that
    # is where it is headed; the others are simply sane ceilings. `services.geocoding_service`
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
    # The place's extent, when the provider sends one. Four named floats rather than a
    # nested object or a list, because that is how a trip location stores them
    # (`schemas.trip.TripLocationInput`) and a client that picks a result writes it
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
