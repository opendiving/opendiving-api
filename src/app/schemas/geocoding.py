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
    # Bounded to the width of `dive_site.location`, since that is where it is headed.
    location: Annotated[str, Field(max_length=255, examples=["Dahab, Egypt"])]
    display_name: Annotated[str, Field(examples=["Blue Hole, Dahab, South Sinai, Egypt"])]
    # The place's own name, where it has one. Absent for a result that is only an address.
    name: str | None = None
    # Carried per-result rather than in an envelope: attribution is a licence condition of
    # the data itself, so it travels with the row it describes and survives a provider
    # swap (it is the provider's own `licence` string when it sends one). The client is
    # expected to render it wherever it shows these results.
    attribution: Annotated[str, Field(examples=["Data © OpenStreetMap contributors, ODbL 1.0."])]
