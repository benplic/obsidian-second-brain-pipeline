"""Model D hub scaffolding and interactive triage."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from second_brain.cli import build_parser, cmd_model_d
from second_brain.ledger import EVENT_TOSSED, UrlLedger
from second_brain.notes import render_manual
from second_brain.model_d.hubs import (
    render_previous_universal_hub,
    render_universal_hub,
    scaffold_hubs,
)
from second_brain.model_d.triage import collect_candidates, run_triage
from second_brain.vault import parse_frontmatter_strict, split_frontmatter


def _card(
    resources: Path,
    folder: str,
    name: str,
    *,
    url: str = "https://example.com/clip",
    status: str = "inbox",
    stage: str | None = None,
    category: str = "Miscellaneous",
    summary: str = "A short summary",
    tags: list[str] | None = None,
    notes: str = "- handwritten note",
) -> Path:
    path = resources / folder / f"{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    tag_lines = "\n".join(f"  - {tag}" for tag in (tags or ["saved-media", "category/miscellaneous"]))
    stage_line = f'stage: "{stage}"\n' if stage else ""
    path.write_text(
        "---\n"
        f'category: "{category}"\n'
        'creator: "sample"\n'
        f'url: "{url}"\n'
        f'status: "{status}"\n'
        f"{stage_line}"
        f'summary: "{summary}"\n'
        "tags:\n"
        f"{tag_lines}\n"
        "---\n"
        f"\n# {name}\n\n## Notes & Insights\n{notes}\n",
        encoding="utf-8",
    )
    return path


def _script(answers: list[str]):
    pending = iter(answers)

    def ask(_prompt: str) -> str:
        return next(pending)

    return ask


def test_scaffold_creates_missing_hubs_and_skips_protected_folders(settings):
    resources = settings.resources_dir
    (resources / "Art & Spaces").mkdir()
    (resources / "Inbox" / "Manual Review").mkdir(parents=True)
    (resources / "Music").mkdir()
    (resources / "Comedy" / "Bits").mkdir(parents=True)
    (resources / ".hidden").mkdir()
    custom = resources / "Comedy" / "Comedy Hub.md"
    custom.write_text("# custom synthesis\n", encoding="utf-8")

    result = scaffold_hubs(resources)

    hub = resources / "Art & Spaces" / "Art & Spaces Hub.md"
    assert hub in result.created
    text = hub.read_text(encoding="utf-8")
    assert 'FROM "3 - Resources/Art & Spaces"' in text
    assert 'status != "tossed"' in text
    assert 'status != "manual-review"' in text
    assert 'stage != "manual-review"' in text
    assert "Synthesis & Scrapbook" in text
    assert "Maintenance Triage Checklist" in text
    assert "[!warning]- Manual review" in text
    assert custom.read_text(encoding="utf-8") == "# custom synthesis\n"
    assert not (resources / "Inbox" / "Inbox Hub.md").exists()
    assert not (resources / "Music" / "Music Hub.md").exists()
    assert not (resources / "Comedy" / "Bits" / "Bits Hub.md").exists()
    assert not (resources / ".hidden" / ".hidden Hub.md").exists()


def test_scaffold_escapes_quotes_in_the_dataview_path():
    text = render_universal_hub('Say "Hi"')
    assert 'FROM "3 - Resources/Say \\"Hi\\""' in text


def test_repair_updates_previous_stock_and_leaves_edits(settings):
    resources = settings.resources_dir
    folder = resources / "Relationships"
    folder.mkdir()
    hub = folder / "Relationships Hub.md"
    hub.write_text(render_previous_universal_hub("Relationships"), encoding="utf-8")

    repaired = scaffold_hubs(resources, repair=True)
    assert hub in repaired.repaired
    assert hub.read_text(encoding="utf-8") == render_universal_hub("Relationships")

    edited = render_previous_universal_hub("Relationships") + "\n- my theme\n"
    hub.write_text(edited, encoding="utf-8")
    again = scaffold_hubs(resources, repair=True)
    assert hub not in again.repaired
    assert hub.read_text(encoding="utf-8") == edited

    untouched = scaffold_hubs(resources, repair=False)
    assert hub.read_text(encoding="utf-8") == edited
    assert hub in untouched.unchanged


def test_repair_upgrades_a_model_c_generic_hub_and_leaves_a_scrapbook_line(settings):
    from second_brain.model_c.hubs import generic_hub

    resources = settings.resources_dir
    folder = resources / "Comedy"
    folder.mkdir()
    hub = folder / "Comedy Hub.md"
    stock = generic_hub("Comedy")
    hub.write_text(stock, encoding="utf-8")

    skipped = scaffold_hubs(resources, repair=False)
    assert hub in skipped.unchanged
    assert hub.read_text(encoding="utf-8") == stock

    repaired = scaffold_hubs(resources, repair=True)
    assert hub in repaired.repaired
    assert hub.read_text(encoding="utf-8") == render_universal_hub("Comedy")

    edited = stock + "\n- my theme\n"
    hub.write_text(edited, encoding="utf-8")
    again = scaffold_hubs(resources, repair=True)
    assert hub not in again.repaired
    assert hub.read_text(encoding="utf-8") == edited


def test_collect_orders_manual_review_before_inbox_and_skips_tossed(settings):
    resources = settings.resources_dir
    _card(resources, "Comedy", "Already tossed", status="tossed")
    inbox = _card(resources, "Comedy", "Needs a look")
    manual = _card(
        resources,
        "Inbox/Manual Review",
        "Broken classification",
        stage="manual-review",
        status="inbox",
    )
    staged = _card(resources, "Art & Spaces", "Hidden problem", status="inbox", stage="manual-review")
    finished = _card(
        resources,
        "Inbox/Manual Review",
        "Already kept",
        status="kept",
        stage="manual-review",
    )
    tossed_staged = _card(resources, "Comedy", "Tossed but staged", status="tossed", stage="manual-review")

    queued = [item.path for item in collect_candidates(resources)]
    assert queued == [manual, staged, inbox]
    assert finished not in queued
    assert tossed_staged not in queued


def test_triage_keep_preserves_the_note_body(settings):
    path = _card(settings.resources_dir, "Comedy", "Keep me", notes="- handwritten note", stage="ready")
    _, body = split_frontmatter(path.read_text(encoding="utf-8"))

    assert run_triage(settings, ask=_script(["k"])) == 0

    updated = path.read_text(encoding="utf-8")
    fm = parse_frontmatter_strict(updated)
    assert fm["status"] == "kept"
    assert fm["stage"] == "ready"
    assert split_frontmatter(updated)[1] == body


def test_keep_files_a_manual_review_card_and_clears_stage(settings):
    resources = settings.resources_dir
    path = resources / "Inbox" / "Manual Review" / "Needs a person.md"
    path.parent.mkdir(parents=True)
    path.write_text(
        render_manual(
            "https://example.com/manual",
            summary="Needs a person",
            category="Comedy/Funny",
        ),
        encoding="utf-8",
    )

    assert run_triage(settings, ask=_script(["k"])) == 0

    dest = resources / "Comedy" / "Needs a person.md"
    assert dest.is_file()
    assert not path.exists()
    fm = parse_frontmatter_strict(dest.read_text(encoding="utf-8"))
    assert fm["status"] == "kept"
    assert "stage" not in fm
    assert collect_candidates(resources) == []


def test_triage_move_updates_category_and_does_not_overwrite(settings):
    resources = settings.resources_dir
    source = _card(
        resources,
        "Inbox/Manual Review",
        "Move me",
        url="https://example.com/move",
        tags=["saved-media", "category/miscellaneous", "topic/jokes"],
        stage="manual-review",
    )
    occupant = resources / "Comedy" / "Move me.md"
    occupant.parent.mkdir(parents=True)
    occupant.write_text("leave this file alone\n", encoding="utf-8")
    _, body = split_frontmatter(source.read_text(encoding="utf-8"))

    assert run_triage(settings, ask=_script(["m", "Comedy"])) == 0

    assert not source.exists()
    assert occupant.read_text(encoding="utf-8") == "leave this file alone\n"
    dest = resources / "Comedy" / "Move me (2).md"
    fm = parse_frontmatter_strict(dest.read_text(encoding="utf-8"))
    assert fm["category"] == "Comedy/Funny"
    assert fm["status"] == "inbox"
    assert "stage" not in fm
    assert fm["tags"] == ["saved-media", "topic/jokes", "category/comedy-funny"]
    assert split_frontmatter(dest.read_text(encoding="utf-8"))[1] == body


def test_triage_move_uses_the_existing_folder_case(settings):
    resources = settings.resources_dir
    source = _card(resources, "Inbox", "Case me", url="https://example.com/case")
    (resources / "Comedy").mkdir()

    assert run_triage(settings, ask=_script(["m", "comedy"])) == 0

    dest = resources / "Comedy" / "Case me.md"
    assert dest.is_file()
    assert not source.exists()
    assert parse_frontmatter_strict(dest.read_text(encoding="utf-8"))["category"] == "Comedy/Funny"


def test_triage_rejects_path_traversal(settings):
    path = _card(settings.resources_dir, "Comedy", "Stay put")
    outside = settings.vault_path.parent / "escaped"
    before = path.read_text(encoding="utf-8")

    assert run_triage(settings, ask=_script(["m", "../escaped", "p", "..\\secret", "s"])) == 0

    assert path.read_text(encoding="utf-8") == before
    assert not outside.exists()
    assert not (settings.vault_path / "1 - Projects").exists()


def test_triage_promote_sanitizes_the_project_folder(settings):
    path = _card(settings.resources_dir, "Comedy", "Ship it", notes="- still here")
    _, body = split_frontmatter(path.read_text(encoding="utf-8"))

    assert run_triage(settings, ask=_script(["p", "Lisbon: trip"])) == 0

    dest = settings.vault_path / "1 - Projects" / "Lisbon trip" / "Ship it.md"
    assert dest.is_file()
    assert not path.exists()
    assert parse_frontmatter_strict(dest.read_text(encoding="utf-8"))["status"] == "promoted"
    assert split_frontmatter(dest.read_text(encoding="utf-8"))[1] == body


def test_triage_toss_hides_and_delete_records_the_ledger(settings):
    resources = settings.resources_dir
    tossed = _card(resources, "Comedy", "Hide me", url="https://example.com/hide", stage="manual-review")
    deleted = _card(resources, "Art & Spaces", "Remove me", url="https://example.com/remove")

    # stage: manual-review is prompted before a plain inbox note.
    assert run_triage(settings, ask=_script(["t", "", "t", "d"])) == 0

    assert tossed.is_file()
    tossed_fm = parse_frontmatter_strict(tossed.read_text(encoding="utf-8"))
    assert tossed_fm["status"] == "tossed"
    assert "stage" not in tossed_fm
    assert collect_candidates(resources) == []
    assert not deleted.exists()
    ledger = UrlLedger(settings.ledger_path)
    assert not ledger.has_event("https://example.com/hide", EVENT_TOSSED)
    assert ledger.has_event("https://example.com/remove", EVENT_TOSSED)


def test_broken_frontmatter_delete_needs_confirmation_and_ledgers_the_url(settings):
    resources = settings.resources_dir
    path = resources / "Inbox" / "Manual Review" / "Broken.md"
    path.parent.mkdir(parents=True)
    path.write_text(
        '---\nurl: "https://example.com/broken"\nstatus: "inbox"\ncategory: [oops\n---\n\n# Broken\n',
        encoding="utf-8",
    )

    assert run_triage(settings, ask=_script(["d", "s"])) == 0
    assert path.is_file()

    assert run_triage(settings, ask=_script(["t", "d"])) == 0
    assert not path.exists()
    assert UrlLedger(settings.ledger_path).has_event("https://example.com/broken", EVENT_TOSSED)


def test_broken_yaml_outside_manual_review_is_queued_but_not_rewritten(settings):
    resources = settings.resources_dir
    path = resources / "Comedy" / "Broken inbox.md"
    path.parent.mkdir(parents=True)
    original = '---\nurl: "https://example.com/comedy"\nstatus: "inbox"\ncategory: [oops\n---\n\n# Broken inbox\n'
    path.write_text(original, encoding="utf-8")

    queued = collect_candidates(resources)
    assert [item.path for item in queued] == [path]
    assert queued[0].fm is None

    assert run_triage(settings, ask=_script(["k", "s"])) == 0
    assert path.read_text(encoding="utf-8") == original


def test_broken_yaml_stage_still_orders_manual_review(settings):
    resources = settings.resources_dir
    inbox = resources / "Comedy" / "Plain broken.md"
    staged = resources / "Art & Spaces" / "Staged broken.md"
    stage_only = resources / "Tips" / "Stage only.md"
    for path in (inbox, staged, stage_only):
        path.parent.mkdir(parents=True, exist_ok=True)
    inbox.write_text(
        '---\nurl: "https://example.com/plain"\nstatus: "inbox"\ncategory: [oops\n---\n\n# Plain\n',
        encoding="utf-8",
    )
    staged.write_text(
        '---\nurl: "https://example.com/staged"\nstatus: "inbox"\nstage: "manual-review"\ncategory: [oops\n---\n\n# Staged\n',
        encoding="utf-8",
    )
    stage_only.write_text(
        '---\nurl: "https://example.com/only"\nstage: "manual-review"\ncategory: [oops\n---\n\n# Only\n',
        encoding="utf-8",
    )
    tossed = resources / "Inbox" / "Manual Review" / "Tossed staged.md"
    tossed.parent.mkdir(parents=True, exist_ok=True)
    tossed.write_text(
        '---\nurl: "https://example.com/tossed"\nstatus: "tossed"\nstage: "manual-review"\ncategory: [oops\n---\n\n# Tossed\n',
        encoding="utf-8",
    )
    promoted = resources / "Inbox" / "Manual Review" / "Promoted.md"
    promoted.write_text(
        '---\nurl: "https://example.com/promoted"\nstatus: "promoted"\ncategory: [oops\n---\n\n# Promoted\n',
        encoding="utf-8",
    )

    queued = [item.path for item in collect_candidates(resources)]
    assert queued == [staged, stage_only, inbox]
    assert all(item.fm is None for item in collect_candidates(resources))
    assert tossed not in queued and promoted not in queued


def test_broken_yaml_with_a_terminal_status_stays_out_of_the_queue(settings):
    resources = settings.resources_dir
    holding = resources / "Inbox" / "Manual Review" / "Done.md"
    holding.parent.mkdir(parents=True)
    holding.write_text(
        '---\nurl: "https://example.com/done"\nstatus: "tossed"\ncategory: [oops\n---\n\n# Done\n',
        encoding="utf-8",
    )
    kept = resources / "Comedy" / "Kept.md"
    kept.parent.mkdir(parents=True)
    kept.write_text(
        '---\nurl: "https://example.com/kept"\nstatus: "kept"\ncategory: [oops\n---\n\n# Kept\n',
        encoding="utf-8",
    )
    assert collect_candidates(resources) == []


def test_triage_skip_and_noninteractive_exit(settings, monkeypatch):
    path = _card(settings.resources_dir, "Comedy", "Later")
    before = path.read_text(encoding="utf-8")
    assert run_triage(settings, ask=_script(["s"])) == 0
    assert path.read_text(encoding="utf-8") == before

    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    assert run_triage(settings) == 2


def test_repair_flag_requires_scaffold(settings):
    with pytest.raises(SystemExit):
        build_parser().parse_args(["model-d"])
    args = build_parser().parse_args(["model-d", "--scaffold", "--repair"])
    assert args.scaffold and args.repair and not args.triage
    assert cmd_model_d(settings, build_parser().parse_args(["model-d", "--triage", "--repair"])) == 2
