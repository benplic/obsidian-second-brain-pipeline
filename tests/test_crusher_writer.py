"""Crusher card writer."""

from __future__ import annotations

from pathlib import Path

from second_brain.crusher.schema import CrusherAnalysis, ExtractedItem
from second_brain.crusher.writer import CRUSHER_END, CRUSHER_START, item_category, write_card_updates

from conftest import make_card


def test_item_category_routes_tip_kinds():
    assert (
        item_category(ExtractedItem(kind="health_tip", name="Sleep hygiene"), "Study Tips")
        == "Fitness & Health"
    )
    assert item_category(ExtractedItem(kind="study_tip", name="Pomodoro"), "Tips") == "Study Tips"
    assert item_category(ExtractedItem(kind="tip", name="Hot glue fix"), "Study Tips") == "Tips"


def test_writer_preserves_notes_and_adds_findings(settings):
    card = make_card(
        settings.resources_dir,
        "Inbox",
        "misc clip",
        "https://www.tiktok.com/@u/video/9001",
    )
    card.write_text(
        card.read_text(encoding="utf-8").replace(
            "# misc clip",
            "# misc clip\n\n## Notes & Insights\n- [ ] Keep this note\n",
        ),
        encoding="utf-8",
    )
    analysis = CrusherAnalysis(
        category="Comedy/Funny",
        confidence=0.88,
        title="Printer standoff",
        summary="Office comedy sketch",
        findings=["Slapstick over a jammed printer"],
        recategorize=True,
    )
    outcome = write_card_updates(
        settings,
        card,
        analysis,
        prompt_version="1",
        crusher_status="done",
        write_children=False,
    )
    text = outcome.parent_path.read_text(encoding="utf-8")
    assert CRUSHER_START in text and CRUSHER_END in text
    assert "Keep this note" in text
    assert 'category: "Comedy/Funny"' in text or "Comedy/Funny" in text
    assert outcome.recategorized


def test_writer_creates_child_items(settings):
    parent = make_card(
        settings.resources_dir,
        "Inbox",
        "travel slides",
        "https://www.tiktok.com/@u/video/9002",
    )
    analysis = CrusherAnalysis(
        category="Travel",
        confidence=0.9,
        title="Europe highlights",
        summary="Three cities in one slideshow",
        items=[
            ExtractedItem(kind="destination", name="Lisbon", location_name="Lisbon, Portugal", position=1),
            ExtractedItem(kind="destination", name="Prague", location_name="Prague, Czechia", position=2),
        ],
    )
    write_card_updates(
        settings,
        parent,
        analysis,
        prompt_version="1",
        crusher_status="done",
        write_children=True,
    )
    travel_children = list((settings.resources_dir / "Travel" / "Europe highlights").glob("*.md"))
    assert len(travel_children) == 2


def test_writer_routes_tip_children_to_tips_and_fitness(settings):
    parent = make_card(
        settings.resources_dir,
        "Study Tips",
        "mixed tips",
        "https://www.tiktok.com/@u/video/9003",
    )
    analysis = CrusherAnalysis(
        category="Tips",
        confidence=0.85,
        title="Handy clips",
        summary="Craft and wellness shortcuts",
        items=[
            ExtractedItem(kind="tip", name="Zip tie cable tidy", position=1),
            ExtractedItem(kind="health_tip", name="Morning stretch routine", position=2),
        ],
    )
    write_card_updates(
        settings,
        parent,
        analysis,
        prompt_version="3",
        crusher_status="done",
        write_children=True,
    )
    craft_children = list((settings.resources_dir / "Tips" / "Handy clips").glob("*.md"))
    health_children = list((settings.resources_dir / "Fitness & Health" / "Handy clips").glob("*.md"))
    assert len(craft_children) == 1
    assert len(health_children) == 1
