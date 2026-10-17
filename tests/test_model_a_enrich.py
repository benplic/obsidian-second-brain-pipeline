"""Model A enrich: data-preserving rewrites."""

from __future__ import annotations

import json

from second_brain.ledger import UrlLedger
from second_brain.model_a.enrich import run_enrich
from second_brain.vault import parse_frontmatter

from conftest import FakeRuntime, rate_limit_error


def _card(settings, folder, name, url, status="inbox", notes="## Notes & Insights\n- my own insight\n"):
    path = settings.resources_dir / folder / f"{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'---\ncategory: "Tech & Coding"\ncreator: "dev"\nurl: "{url}"\nstatus: "{status}"\n---\n\n'
                    f"# {name}\n\n> **Summary:** a thing\n\n{notes}", encoding="utf-8")
    return path


def _analysis(item_id, **kw):
    base = {"item_id": item_id, "category": "Tech & Coding", "summary": f"Useful tip {item_id}",
            "confidence": 0.9, "needs_manual_review": False, "tech_tool": "Python"}
    return {**base, **kw}


def test_matches_by_item_id_and_preserves_notes_and_status(settings):
    _card(settings, "Tech & Coding", "old a", "https://t.co/a", status="kept")
    _card(settings, "Tech & Coding", "old b", "https://t.co/b")
    # Response deliberately out of order.
    resp = json.dumps({"items": [_analysis("1", summary="Second"), _analysis("0", summary="First")]})
    result = run_enrich(settings, runtime=FakeRuntime([resp]))
    assert result.enriched == 2

    first = (settings.resources_dir / "Tech & Coding" / "First.md").read_text(encoding="utf-8")
    fm = parse_frontmatter(first)
    assert fm["url"] == "https://t.co/a" and fm["status"] == "kept" and "tool/python" in fm["tags"]
    assert "- my own insight" in first
    assert not (settings.resources_dir / "Tech & Coding" / "old a.md").exists()


def test_failed_batch_leaves_cards_untouched(settings, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    path = _card(settings, "Tech & Coding", "keep me", "https://t.co/a")
    before = path.read_text(encoding="utf-8")
    result = run_enrich(settings, runtime=FakeRuntime([rate_limit_error() for _ in range(5)]))
    assert result.stopped_reason and result.enriched == 0
    assert path.read_text(encoding="utf-8") == before
    assert not (settings.resources_dir / "Inbox").exists()


def test_movie_merge_records_url_in_ledger(settings):
    movies = settings.resources_dir / "Movies & Shows"
    _card(settings, "Movies & Shows", "Existing Film", "https://t.co/first")
    (movies / "Existing Film.md").write_text('---\nurl: "https://t.co/first"\nconfidence: 0.9\n---\n# Existing Film\n', encoding="utf-8")
    _card(settings, "Movies & Shows", "clip", "https://t.co/clip")
    resp = json.dumps({"items": [_analysis("0", category="Movies & Shows", extracted_movie_title="Existing Film")]})
    result = run_enrich(settings, runtime=FakeRuntime([resp]))
    assert result.merged == 1
    assert "https://t.co/clip" in (movies / "Existing Film.md").read_text(encoding="utf-8")
    assert not (movies / "clip.md").exists()
    assert UrlLedger(settings.ledger_path).has_event("https://t.co/clip", "merged")
