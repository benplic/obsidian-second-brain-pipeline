"""Card contract migration. Dry-run writes nothing."""

from __future__ import annotations

from second_brain.migrate_cards import run_migrate_cards
from second_brain.model_c.hubs import FITNESS_HUB, PREVIOUS_FITNESS_HUB
from second_brain.vault import parse_frontmatter


def _note(settings, folder: str, name: str, body: str):
    path = settings.resources_dir / folder / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def test_dry_run_does_not_write(settings):
    path = _note(
        settings,
        "Music",
        "track.md",
        '---\ncategory: "Music"\nstatus: "ready"\ntags:\n  - second-brain\n  - category/recipes-&-food\n---\n\n# track\n',
    )
    before = path.read_text(encoding="utf-8")
    count = run_migrate_cards(settings, apply=False)
    assert count == 1
    assert path.read_text(encoding="utf-8") == before


def test_apply_moves_ready_to_stage_and_legalizes_tags(settings):
    path = _note(
        settings,
        "Music",
        "track.md",
        '---\ncategory: "Music"\ncreator: "a"\nurl: "https://example.com/a"\nstatus: "ready"\ntags:\n  - second-brain\n  - category/recipes-&-food\n---\n\n# track\n',
    )
    kept = _note(
        settings,
        "Comedy",
        "kept.md",
        '---\ncategory: "Comedy/Funny"\nurl: "https://example.com/k"\nstatus: "kept"\ntags:\n  - saved-media\n---\n\n# kept\n',
    )
    count = run_migrate_cards(settings, apply=True)
    assert count >= 1
    fm = parse_frontmatter(path.read_text(encoding="utf-8"))
    assert fm["status"] == "inbox"
    assert fm["stage"] == "ready"
    assert fm["category"] == "Saved Music"
    assert "saved-media" in fm["tags"]
    assert "second-brain" not in fm["tags"]
    assert "category/recipes-and-food" in fm["tags"]
    assert parse_frontmatter(kept.read_text(encoding="utf-8"))["status"] == "kept"


def test_apply_refreshes_a_previous_stock_hub(settings):
    path = _note(settings, "Fitness & Health", "Fitness Hub.md", PREVIOUS_FITNESS_HUB)
    run_migrate_cards(settings, apply=True)
    assert path.read_text(encoding="utf-8") == FITNESS_HUB
