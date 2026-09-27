"""Travel schema migration: backfill Map View fields on legacy cards."""

from __future__ import annotations

from pathlib import Path

from second_brain.migrate_travel import (
    extract_summary_from_body,
    format_frontmatter,
    merge_travel_defaults,
    migrate_card_content,
    run_migrate_travel,
)
from second_brain.vault import parse_frontmatter


LEGACY_CARD = """---
category: "Travel"
creator: "old.creator"
url: "https://t.co/travel/legacy"
tags:
  - tiktok
  - category/travel
---

# Old travel clip

> **Summary:** Niche bucket list spots in Lisbon

- **Creator:** @old.creator
- **Source Link:** [https://t.co/travel/legacy](https://t.co/travel/legacy)

## Notes & Insights
- Keep my note
"""


def _write_travel_card(settings, name: str, content: str) -> Path:
    folder = settings.resources_dir / "Travel"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{name}.md"
    path.write_text(content, encoding="utf-8")
    return path


def test_extract_summary_from_body():
    assert extract_summary_from_body(LEGACY_CARD) == "Niche bucket list spots in Lisbon"
    assert extract_summary_from_body("# No summary\n") == ""


def test_merge_travel_defaults_fills_missing_only():
    from second_brain.vault import split_frontmatter

    fm = parse_frontmatter(LEGACY_CARD)
    _, body = split_frontmatter(LEGACY_CARD)
    merged, changed = merge_travel_defaults(fm, body)
    assert set(changed) >= {"status", "summary", "location", "location_name", "weight", "mapMarkerColor", "tags"}
    assert merged["status"] == "inbox"
    assert merged["summary"] == "Niche bucket list spots in Lisbon"
    assert merged["location"] == ""
    assert merged["weight"] == 0
    assert merged["mapMarkerColor"] == "#9E9E9E"
    assert "saved-media" in merged["tags"] and "tiktok" in merged["tags"]

    # Second pass is a no-op.
    _, changed2 = merge_travel_defaults(merged, body)
    assert changed2 == []


def test_merge_preserves_existing_weight_and_location():
    fm = {
        "category": "Travel",
        "status": "kept",
        "summary": "Already set",
        "location": [35.0, 139.0],
        "location_name": "Tokyo",
        "weight": 4,
        "mapMarkerColor": "#FFA726",
        "tags": ["saved-media"],
    }
    merged, changed = merge_travel_defaults(fm, "")
    assert changed == []
    assert merged["weight"] == 4
    assert merged["location"] == [35.0, 139.0]


def test_migrate_card_content_round_trip():
    new_content, changed = migrate_card_content(LEGACY_CARD)
    assert new_content is not None
    assert "weight" in changed
    fm = parse_frontmatter(new_content)
    assert fm["weight"] == 0
    assert "Keep my note" in new_content
    assert migrate_card_content(new_content) == (None, [])


def test_format_frontmatter_location_list():
    text = format_frontmatter({"location": [1.5, 2.5], "weight": 0})
    assert "location: [1.5, 2.5]" in text
    assert "weight: 0" in text


def test_dry_run_does_not_write(settings):
    path = _write_travel_card(settings, "Legacy", LEGACY_CARD)
    before = path.read_text(encoding="utf-8")
    result = run_migrate_travel(settings, apply=False)
    assert result.dry_run and result.updated == 1
    assert path.read_text(encoding="utf-8") == before
    assert not (settings.resources_dir / "Travel" / "Travel Map.md").exists()


def test_apply_updates_cards_and_creates_scaffold(settings):
    path = _write_travel_card(settings, "Legacy", LEGACY_CARD)
    result = run_migrate_travel(settings, apply=True)
    assert result.updated == 1 and result.hub_created and result.map_created
    fm = parse_frontmatter(path.read_text(encoding="utf-8"))
    assert fm["status"] == "inbox"
    assert fm["mapMarkerColor"] == "#9E9E9E"
    assert (settings.resources_dir / "Travel" / "Travel Hub.md").is_file()
    assert (settings.resources_dir / "Travel" / "Travel Map.md").is_file()

    # Idempotent.
    result2 = run_migrate_travel(settings, apply=True)
    assert result2.updated == 0 and result2.skipped_ok == 1
    assert not result2.hub_created and not result2.map_created


def test_force_templates_overwrites_hub(settings):
    travel = settings.resources_dir / "Travel"
    travel.mkdir(parents=True)
    hub = travel / "Travel Hub.md"
    hub.write_text("# Custom hub\n", encoding="utf-8")
    run_migrate_travel(settings, apply=True, force_templates=True)
    assert "Destination Matrix" in hub.read_text(encoding="utf-8")
