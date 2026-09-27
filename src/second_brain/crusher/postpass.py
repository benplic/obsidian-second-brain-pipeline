"""Vault-wide post-processing after crusher writes."""

from __future__ import annotations

import json
import logging
import time
from collections import Counter
from pathlib import Path
from urllib.error import URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from ..config import Settings
from ..io_utils import atomic_write_text
from ..model_a.enrich import add_to_kanban
from ..steps.write_cards import map_marker_color_for_weight
from ..vault import parse_frontmatter_strict, rebuild_card

logger = logging.getLogger(__name__)

_GEO_CACHE: dict[str, tuple[float, float] | None] = {}


def _nominatim_geocode(query: str, settings: Settings) -> tuple[float, float] | None:
    if settings.crusher.geocoder == "none":
        return None
    key = query.strip().lower()
    if key in _GEO_CACHE:
        return _GEO_CACHE[key]
    url = f"https://nominatim.openstreetmap.org/search?q={quote(query)}&format=json&limit=1"
    req = Request(url, headers={"User-Agent": settings.crusher.geocoder_user_agent})
    try:
        with urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (URLError, OSError, json.JSONDecodeError, ValueError) as exc:
        logger.debug("Geocode failed for %r: %s", query, exc)
        _GEO_CACHE[key] = None
        return None
    time.sleep(1.0)  # Nominatim usage policy: max 1 req/s
    if not data:
        _GEO_CACHE[key] = None
        return None
    try:
        lat = float(data[0]["lat"])
        lon = float(data[0]["lon"])
    except (KeyError, TypeError, ValueError):
        _GEO_CACHE[key] = None
        return None
    _GEO_CACHE[key] = (lat, lon)
    return lat, lon


def recompute_travel_weights(settings: Settings) -> int:
    travel_dir = settings.resources_dir / "Travel"
    if not travel_dir.is_dir():
        return 0
    paths = [p for p in travel_dir.rglob("*.md") if p.is_file() and "Hub" not in p.name and "Map" not in p.name]
    names: list[str] = []
    parsed: list[tuple[Path, dict]] = []
    for path in paths:
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            continue
        fm = parse_frontmatter_strict(content)
        if not fm:
            continue
        loc_name = str(fm.get("location_name") or "").strip()
        if not loc_name:
            continue
        names.append(loc_name)
        parsed.append((path, fm))

    counts = Counter(names)
    updated = 0
    for path, fm in parsed:
        loc_name = str(fm.get("location_name") or "").strip()
        weight = counts[loc_name]
        if int(fm.get("weight") or 0) == weight and fm.get("mapMarkerColor") == map_marker_color_for_weight(weight):
            continue
        fm["weight"] = weight
        fm["mapMarkerColor"] = map_marker_color_for_weight(weight)
        if not fm.get("location") and settings.crusher.geocoder != "none":
            coords = _nominatim_geocode(loc_name, settings)
            if coords:
                fm["location"] = [coords[0], coords[1]]
        try:
            content = path.read_text(encoding="utf-8")
            atomic_write_text(path, rebuild_card(content, fm))
            updated += 1
        except (OSError, ValueError) as exc:
            logger.warning("Could not update travel weight for %s: %s", path.name, exc)
    return updated


def add_movie_children_to_kanban(settings: Settings) -> int:
    movies_dir = settings.resources_dir / "Movies & Shows"
    if not movies_dir.is_dir():
        return 0
    added = 0
    for path in movies_dir.rglob("*.md"):
        if path.name.endswith("Kanban.md"):
            continue
        fm = parse_frontmatter_strict(path.read_text(encoding="utf-8"))
        if not fm or fm.get("item_kind") != "movie":
            continue
        title = path.stem
        before = (movies_dir / "!Watchlist Kanban.md").read_text(encoding="utf-8") if (movies_dir / "!Watchlist Kanban.md").exists() else ""
        add_to_kanban(settings.resources_dir, title)
        after = (movies_dir / "!Watchlist Kanban.md").read_text(encoding="utf-8") if (movies_dir / "!Watchlist Kanban.md").exists() else ""
        if after != before:
            added += 1
    return added


def run_postpass(settings: Settings) -> tuple[int, int]:
    travel = recompute_travel_weights(settings)
    kanban = add_movie_children_to_kanban(settings)
    logger.info("Crusher post-pass: %d travel card(s) reweighted, %d Kanban add(s).", travel, kanban)
    return travel, kanban
