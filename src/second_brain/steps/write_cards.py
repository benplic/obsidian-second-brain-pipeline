"""Step 3: organized_tiktoks.csv -> Markdown cards in the vault.

Per card: write (temp + fsync + replace) -> record URL in ledger -> pop the
row from the CSV (atomic rewrite). The original rewrote the CSV only after the
whole loop, so a crash mid-run replayed every row; and because it deduped by
file name (``<title> (<row>).md``) rather than URL, replayed or unrelated
same-titled rows could be skipped as "done" or duplicated.

Travel cards also carry Map View fields (``location``, ``weight``,
``mapMarkerColor``) so a later enrichment script can geocode places and
re-tint pins without changing the card template again.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ..config import Settings
from ..io_utils import atomic_write_csv, read_csv_rows, remove_if_exists
from ..ledger import EVENT_CARDED, UrlLedger
from ..taxonomy import DEFAULT_CATEGORY, category_tag, folder_for
from ..urls import normalize_url
from ..vault import get_vault_urls, sanitize_filename, write_card, yaml_str
from .categorize import CSV_FIELDS

logger = logging.getLogger(__name__)

# Obsidian Map View pin colors by how often a place shows up across media.
# Future enrichment scripts should call map_marker_color_for_weight() after
# recalculating weight so pin styling stays consistent with new cards.
TRAVEL_CATEGORY = "Travel"
_WEIGHT_TIER_COLORS: tuple[tuple[int, str], ...] = (
    (6, "#EF5350"),   # Hotspot / must-visit
    (3, "#FFA726"),   # Trending / frequent
    (1, "#42A5F5"),   # Noticed
)
_WEIGHT_ZERO_COLOR = "#9E9E9E"  # Unprocessed / single mention (muted grey)


@dataclass
class WriteCardsResult:
    created: int = 0
    skipped_known: int = 0
    failed: int = 0


def map_marker_color_for_weight(weight: int) -> str:
    """Return the Map View pin color for a Travel card weight.

    Tiers: 0 grey, 1-2 blue, 3-5 orange, 6+ red. Negative weights are treated
    as zero so a bad enrichment pass cannot invent an unknown color.
    """
    try:
        value = int(weight)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Travel weight must be an integer, got {weight!r}") from exc
    if value < 0:
        # TODO: Decide whether enrichment should clamp or reject negatives.
        value = 0
    if value == 0:
        return _WEIGHT_ZERO_COLOR
    for minimum, color in _WEIGHT_TIER_COLORS:
        if value >= minimum:
            return color
    return _WEIGHT_ZERO_COLOR


def _travel_frontmatter_lines(summary: str) -> str:
    """Default Map View / weighting keys for a new Travel card (weight 0).

    ``summary`` is duplicated into frontmatter so the Travel Hub Dataview
    table can show "Core Idea" without scraping the note body.
    """
    color = map_marker_color_for_weight(0)
    return (
        f"summary: {yaml_str(summary)}\n"
        'location: ""  # [lat, lng] or "City, Country" for Map View\n'
        'location_name: ""  # e.g. "Shibuya, Tokyo" (filled by enrichment)\n'
        "weight: 0  # times this location is referenced across saved media\n"
        f'mapMarkerColor: "{color}"  # pin color; see map_marker_color_for_weight()'
    )


def render_card(row: dict) -> tuple[str, str, str]:
    """Return (category, title, markdown) for one CSV row.

    Template and frontmatter match the original script so existing Dataview
    queries keep working; values are now YAML-escaped. Travel cards get
    extra location/weight keys for the Map View plugin.
    """
    category = row.get("Category") or DEFAULT_CATEGORY
    title = row.get("Title") or row.get("Summary") or "Saved Post"
    creator = row.get("Creator") or "Unknown"
    url = row.get("URL", "")
    summary = row.get("Summary") or ""
    # Travel-only block keeps non-Travel cards unchanged for Dataview compat.
    travel_block = (
        f"\n{_travel_frontmatter_lines(summary)}" if category == TRAVEL_CATEGORY else ""
    )
    content = f"""---
category: {yaml_str(category)}
creator: {yaml_str(creator)}
url: {yaml_str(url)}
status: "inbox"
tags:
  - saved-media
  - {category_tag(category)}{travel_block}
---

# {title}

> **Summary:** {summary}

- **Creator:** @{creator}
- **Source:** [{url}]({url})
- **Original Tags:** {row.get('Tags') or 'None'}

## Notes & Insights
- 
"""
    return category, title, content


def run_write_cards(settings: Settings) -> WriteCardsResult:
    result = WriteCardsResult()
    csv_path = settings.organized_csv_path
    if not csv_path.exists():
        logger.info("Queue empty: '%s' not found. Skipping Obsidian transfer.", csv_path.name)
        return result

    settings.require_vault()
    fieldnames, rows = read_csv_rows(csv_path)
    fieldnames = fieldnames or CSV_FIELDS
    ledger = UrlLedger(settings.ledger_path)
    vault_urls = get_vault_urls(settings.resources_dir)
    known = vault_urls | ledger.urls
    failed_rows: list[dict] = []

    for position, row in enumerate(rows):
        url = normalize_url(row.get("URL", ""))
        if url and url in known:
            # Card already exists (e.g. crash after write, before pop) or the
            # URL was tossed/deleted earlier. Count it as done.
            result.skipped_known += 1
            if url in vault_urls and url not in ledger:
                ledger.record(url, EVENT_CARDED, source="write-cards")
        else:
            category, title, content = render_card(row)
            target_dir = settings.resources_dir / folder_for(category, settings.folder_map)
            try:
                path = write_card(target_dir, sanitize_filename(title), content)
            except OSError as exc:
                # Keep the row for retry (original behavior) and move on.
                logger.error("Failed to create card for %s: %s", url or title, exc)
                failed_rows.append(row)
                result.failed += 1
            else:
                result.created += 1
                logger.debug("Created %s", path)
                if url:
                    ledger.record(url, EVENT_CARDED, category=category, source="write-cards")
                    known.add(url)
                else:
                    # TODO: Rows without a URL cannot be deduped; decide whether to drop them upstream.
                    logger.warning("Card '%s' has no URL; it cannot be deduped on future runs.", path.name)

        # Pop on success: the CSV now holds only failed rows + rows not yet visited.
        remaining = failed_rows + rows[position + 1:]
        if remaining:
            atomic_write_csv(csv_path, fieldnames, remaining)

    logger.info("Generated %d new card(s) in The Brain (%d already known).", result.created, result.skipped_known)
    if failed_rows:
        logger.warning("Kept %d failed row(s) in '%s' for retry.", len(failed_rows), csv_path.name)
    elif remove_if_exists(csv_path):
        logger.info("CLEANUP: All cards generated. '%s' deleted.", csv_path.name)
    return result
