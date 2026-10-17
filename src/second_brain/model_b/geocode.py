"""Resolve a place name with Nominatim.

One process-wide rate limiter enforces Nominatim's one-request-per-second
policy (we wait 1.1s). A timeout or an empty result is a miss, not a failed run.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass

from geopy.exc import GeocoderServiceError, GeocoderTimedOut
from geopy.extra.rate_limiter import RateLimiter
from geopy.geocoders import Nominatim

from ..config import Settings

logger = logging.getLogger(__name__)

# Nominatim usage policy: at most 1 request per second. 1.1s leaves a margin.
_MIN_DELAY_SECONDS = 1.1
_limiters: dict[str, RateLimiter] = {}


@dataclass(frozen=True)
class GeocodeHit:
    latitude: float
    longitude: float
    address: str


def coordinates_ok(latitude: float, longitude: float) -> bool:
    return (
        math.isfinite(latitude)
        and math.isfinite(longitude)
        and -90.0 <= latitude <= 90.0
        and -180.0 <= longitude <= 180.0
    )


def _limiter(user_agent: str) -> RateLimiter:
    """Reuse one limiter per user agent so the delay applies across URLs."""
    existing = _limiters.get(user_agent)
    if existing is not None:
        return existing
    # swallow_exceptions is off so the caller can log the specific failure.
    # TODO: If Nominatim starts requiring an API key, switch the geocoder here
    # and keep RateLimiter so the rest of Model B does not change.
    limiter = RateLimiter(
        Nominatim(user_agent=user_agent, timeout=15).geocode,
        min_delay_seconds=_MIN_DELAY_SECONDS,
        max_retries=1,
        error_wait_seconds=2.0,
        swallow_exceptions=False,
    )
    _limiters[user_agent] = limiter
    return limiter


def geocode_place(query: str, settings: Settings) -> GeocodeHit | None:
    """Return coordinates and a formatted address, or None when unresolved.

    ``crusher.geocoder: none`` skips the network call. The query is a place
    name, never a file path or a URL from the queue.
    """
    text = " ".join((query or "").split())
    if not text or settings.crusher.geocoder == "none":
        return None
    user_agent = (settings.crusher.geocoder_user_agent or "").strip()
    if not user_agent:
        logger.warning("Geocoder user agent is empty; skipping Nominatim for %r.", text)
        return None
    try:
        # addressdetails fills the structured record; Location.address is the display string.
        location = _limiter(user_agent)(text, addressdetails=True, language="en")
    except (GeocoderTimedOut, GeocoderServiceError, OSError) as exc:
        logger.warning("Geocode failed for %r: %s", text, exc)
        return None
    if location is None:
        logger.info("No geocode hit for %r.", text)
        return None
    try:
        latitude = float(location.latitude)
        longitude = float(location.longitude)
    except (TypeError, ValueError) as exc:
        logger.warning("Geocode for %r had no coordinates: %s", text, exc)
        return None
    if not coordinates_ok(latitude, longitude):
        logger.warning("Geocode for %r returned out-of-range coordinates.", text)
        return None
    address = " ".join(str(getattr(location, "address", "") or "").split())
    return GeocodeHit(latitude=latitude, longitude=longitude, address=address)


def safe_geocode(geocode, query: str) -> GeocodeHit | None:
    """Call ``geocode(query)`` and treat geocoder failures as a miss.

    Tests inject a raising callable. Production passes :func:`geocode_place`
    bound to settings, which already returns None on the same errors.
    """
    try:
        hit = geocode(query)
    except (GeocoderTimedOut, GeocoderServiceError, OSError) as exc:
        logger.warning("Geocode failed for %r: %s", query, exc)
        return None
    if hit is None:
        return None
    if not isinstance(hit, GeocodeHit) or not coordinates_ok(hit.latitude, hit.longitude):
        logger.warning("Ignoring unusable geocode result for %r.", query)
        return None
    return hit
