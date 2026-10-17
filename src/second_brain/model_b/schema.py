"""Structured travel extraction. Classification is not this model's job.

``status`` on the card stays human triage (``inbox``). The outcome of this
decision lives in ``stage`` so a later ``kept`` / ``tossed`` edit does not
erase whether the pin was mapped.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

TRAVEL = "Travel"

STAGE_MAPPED = "mapped"
STAGE_UNMAPPED = "unmapped-travel"
STAGE_MANUAL = "manual-review"

# Tag slugs are shorter than the stage value so Dataview filters stay readable.
_ROUTE_TAGS = {
    STAGE_MAPPED: "travel/mapped",
    STAGE_UNMAPPED: "travel/unmapped",
    STAGE_MANUAL: "travel/manual-review",
}


class TravelExtraction(BaseModel):
    """Place facts pulled from a clip that was already classified as travel."""

    is_travel_related: bool = Field(description="True when the clip is about a place, trip, or landmark.")
    place_name: str | None = Field(
        default=None,
        description="City, landmark, restaurant, or region a geocoder can resolve. Null when unknown.",
    )
    country: str | None = Field(default=None, description="Country, when the clip names one.")
    summary: str = Field(description="5-10 word summary of highlights or tips.")
    confidence: float = Field(ge=0.0, le=1.0, description="Confidence in the extraction, from 0.0 to 1.0.")


def place_query(extraction: TravelExtraction) -> str | None:
    """Nominatim query. Never a filesystem path."""
    place = " ".join((extraction.place_name or "").split())
    if not place:
        return None
    country = " ".join((extraction.country or "").split())
    if country and country.casefold() not in place.casefold():
        return f"{place}, {country}"
    return place


def route_tag(stage: str) -> str:
    return _ROUTE_TAGS.get(stage, _ROUTE_TAGS[STAGE_MANUAL])


def route_travel(extraction: TravelExtraction, *, geocoded: bool, threshold: float) -> str:
    """Pipeline stage for one extraction.

    Low confidence and non-travel clips are manual review even if a geocoder
    happened to return coordinates. A confirmed place with no coordinates stays
    in Travel as ``unmapped-travel`` so it can be pinned by hand.
    """
    if not extraction.is_travel_related or extraction.confidence < threshold:
        return STAGE_MANUAL
    if geocoded:
        return STAGE_MAPPED
    return STAGE_UNMAPPED
