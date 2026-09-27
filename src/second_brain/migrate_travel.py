"""One-time Travel schema migration for existing vault cards.

Legacy Travel notes often lack Map View fields (``location``, ``weight``,
``mapMarkerColor``), ``summary``, and ``status``. This module backfills those
keys without clobbering values a human (or later enrichment) already set, and
ensures ``Travel Hub.md`` / ``Travel Map.md`` exist under
``3 - Resources/Travel/``.

Default mode is dry-run. Pass ``apply=True`` (CLI ``--apply``) to write.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from .config import Settings
from .io_utils import atomic_write_text
from .steps.write_cards import TRAVEL_CATEGORY, map_marker_color_for_weight
# format_frontmatter / rebuild_card live in vault.py (shared with the crusher);
# re-exported here so existing imports keep working.
from .vault import format_frontmatter, parse_frontmatter, rebuild_card, split_frontmatter

logger = logging.getLogger(__name__)

TRAVEL_FOLDER = "Travel"
HUB_FILENAME = "Travel Hub.md"
MAP_FILENAME = "Travel Map.md"

# Skip board/hub notes when scanning cards.
_SKIP_NAME_SUFFIXES = ("Hub.md", "Map.md", "Kanban.md")

_SUMMARY_BODY_RE = re.compile(r"^> \*\*Summary:\*\*\s*(.+)$", re.MULTILINE)

TRAVEL_HUB_TEMPLATE = """# Travel Hub & Destination Matrix

Interactive destination matrix for saved travel media. Cards carry Map View
fields (`location`, `location_name`, `weight`, `mapMarkerColor`). Pin color
follows weight: grey (0) → blue (1–2) → orange (3–5) → red (6+).

Triage cards here: set `status` to `kept`, `promoted`, or `tossed` in each
card's frontmatter. Run `second-brain ledger-sync` before deleting tossed
cards so they are never re-ingested.

Open [[Travel Map]] for the embedded Map View of geocoded pins.

## 1. Hotspots & Frequent Recommendations

```dataview
TABLE
  location_name as "Location",
  weight as "References",
  summary as "Core Idea",
  creator as "Curator",
  status as "Status"
FROM "3 - Resources/Travel"
WHERE file.name != this.file.name AND status != "tossed"
SORT weight desc, file.ctime desc
```

## 2. Location Synthesis & Cluster Notes

*Group recurring recommendations by destination before promoting them to active itineraries:*

- **Tokyo:**
- **New York:**
- **Other:**

## 3. Trip Promotion Workflow

When planning a specific trip:

1. Create `1 - Projects/Trip - [Destination]`.
2. Move or transclude relevant cards from `3 - Resources/Travel/` into the project itinerary (`![[Card Name]]`).
3. Set card status to `status: "promoted"`.
"""

TRAVEL_MAP_TEMPLATE = """# Travel Map

Embedded [Map View](https://github.com/esm7/obsidian-map-view) of notes under
`3 - Resources/Travel`. Pins appear once a card's `location` is set to
`[lat, lng]` (or `"lat,lng"`). Marker color follows `mapMarkerColor` / weight.

Requires the **Map View** community plugin. Until enrichment geocodes places,
the map may be empty even though cards carry the schema.

```mapview
{"name":"Travel Destinations","query":"path:\\"3 - Resources/Travel\\"","autoFit":true,"embeddedHeight":600}
```

See also: [[Travel Hub]]
"""


@dataclass
class MigrateTravelResult:
    scanned: int = 0
    updated: int = 0
    skipped_ok: int = 0
    failed: int = 0
    hub_created: bool = False
    map_created: bool = False
    dry_run: bool = True
    updated_paths: list[str] = field(default_factory=list)
    failed_paths: list[str] = field(default_factory=list)


def travel_dir(settings: Settings) -> Path:
    return settings.resources_dir / TRAVEL_FOLDER


def is_travel_card_path(path: Path) -> bool:
    """True for Markdown cards in Travel/, excluding hub/map/kanban notes."""
    if not path.name.endswith(".md") or path.name.startswith("."):
        return False
    return not any(path.name.endswith(suffix) for suffix in _SKIP_NAME_SUFFIXES)


def extract_summary_from_body(body: str) -> str:
    """Pull the blockquote summary line from a card body, if present."""
    match = _SUMMARY_BODY_RE.search(body or "")
    return match.group(1).strip() if match else ""


def _as_int_weight(value: object) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _is_blank(value: object) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def merge_travel_defaults(fm: dict, body: str) -> tuple[dict, list[str]]:
    """Return (merged_frontmatter, list of keys that were added or filled).

    Never overwrites a non-blank existing value (except syncing mapMarkerColor
    when it is missing). Weight defaults to 0; color follows the weight tier.
    """
    merged = dict(fm)
    changed: list[str] = []

    if _is_blank(merged.get("status")):
        merged["status"] = "inbox"
        changed.append("status")

    if _is_blank(merged.get("summary")):
        summary = extract_summary_from_body(body)
        merged["summary"] = summary
        changed.append("summary")

    if "location" not in merged:
        merged["location"] = ""
        changed.append("location")

    if "location_name" not in merged:
        merged["location_name"] = ""
        changed.append("location_name")

    if "weight" not in merged:
        merged["weight"] = 0
        changed.append("weight")

    weight = _as_int_weight(merged.get("weight", 0))
    merged["weight"] = weight
    expected_color = map_marker_color_for_weight(weight)
    if _is_blank(merged.get("mapMarkerColor")):
        merged["mapMarkerColor"] = expected_color
        changed.append("mapMarkerColor")

    # Ensure tags is a list when present; do not invent tags for notes that
    # somehow lack them (rare; leave as-is for human review).
    tags = merged.get("tags")
    if isinstance(tags, list) and "saved-media" not in tags and "tiktok" in tags:
        # Legacy exports used tiktok; keep it and add the pipeline's canonical tag.
        merged["tags"] = list(tags) + ["saved-media"]
        changed.append("tags")

    return merged, changed


def migrate_card_content(content: str) -> tuple[str | None, list[str]]:
    """Return (new_content or None if unchanged, changed_keys)."""
    fm = parse_frontmatter(content)
    if not fm:
        return None, []
    _, body = split_frontmatter(content)
    merged, changed = merge_travel_defaults(fm, body or "")
    if not changed:
        return None, []
    return rebuild_card(content, merged), changed


def ensure_scaffold(travel_path: Path, *, apply: bool, force_templates: bool) -> tuple[bool, bool]:
    """Create Travel Hub / Travel Map when missing (or when force_templates)."""
    hub_path = travel_path / HUB_FILENAME
    map_path = travel_path / MAP_FILENAME
    hub_needed = force_templates or not hub_path.exists()
    map_needed = force_templates or not map_path.exists()

    if apply:
        travel_path.mkdir(parents=True, exist_ok=True)
        if hub_needed:
            existed = hub_path.exists()
            atomic_write_text(hub_path, TRAVEL_HUB_TEMPLATE)
            logger.info("%s %s", "Overwrote" if existed else "Created", hub_path.name)
        if map_needed:
            existed = map_path.exists()
            atomic_write_text(map_path, TRAVEL_MAP_TEMPLATE)
            logger.info("%s %s", "Overwrote" if existed else "Created", map_path.name)
    else:
        if hub_needed:
            logger.info("[dry-run] Would create %s", hub_path)
        if map_needed:
            logger.info("[dry-run] Would create %s", map_path)

    return hub_needed, map_needed


def run_migrate_travel(
    settings: Settings,
    *,
    apply: bool = False,
    force_templates: bool = False,
) -> MigrateTravelResult:
    """Scan Travel cards and backfill Map View / weighting frontmatter."""
    result = MigrateTravelResult(dry_run=not apply)
    settings.require_vault()
    travel_path = travel_dir(settings)

    hub_needed, map_needed = ensure_scaffold(travel_path, apply=apply, force_templates=force_templates)
    result.hub_created = hub_needed and apply
    result.map_created = map_needed and apply

    if not travel_path.is_dir():
        logger.warning("Travel folder not found: %s", travel_path)
        return result

    for path in sorted(travel_path.glob("*.md")):
        if not is_travel_card_path(path):
            continue
        result.scanned += 1
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            logger.warning("Skipping unreadable card %s: %s", path.name, exc)
            result.failed += 1
            result.failed_paths.append(path.name)
            continue

        try:
            new_content, changed = migrate_card_content(content)
        except ValueError as exc:
            logger.warning("Skipping %s: %s", path.name, exc)
            result.failed += 1
            result.failed_paths.append(path.name)
            continue

        if not new_content:
            result.skipped_ok += 1
            continue

        result.updated += 1
        result.updated_paths.append(path.name)
        if apply:
            try:
                atomic_write_text(path, new_content)
            except OSError as exc:
                logger.error("Failed to write %s: %s", path.name, exc)
                result.failed += 1
                result.failed_paths.append(path.name)
                result.updated -= 1
                continue
            logger.debug("Updated %s (+%s)", path.name, ", ".join(changed))
        else:
            logger.info("[dry-run] Would update %s (+%s)", path.name, ", ".join(changed))

    mode = "Applied" if apply else "Dry-run"
    logger.info(
        "%s travel migration: scanned=%d updated=%d already_ok=%d failed=%d hub=%s map=%s",
        mode,
        result.scanned,
        result.updated,
        result.skipped_ok,
        result.failed,
        "created" if result.hub_created else ("needed" if hub_needed else "ok"),
        "created" if result.map_created else ("needed" if map_needed else "ok"),
    )
    if not apply and (result.updated or hub_needed or map_needed):
        logger.info("Re-run with --apply to write changes.")
    return result
