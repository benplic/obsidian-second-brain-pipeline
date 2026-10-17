"""Model C ingest: routing, notes, dedupe, dead links, hubs. No network."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from second_brain.config import ConfigError, settings_from_dict
from second_brain.ledger import UrlLedger
from second_brain.model_c.hubs import FITNESS_HUB, HUBS, MUSIC_HUB, RECIPES_HUB, ensure_hubs
from second_brain.model_c.ingest import render_note, run_ingest, transcript_needs_vision
from second_brain.model_c.schema import ModelCAnalysis
from second_brain.vault import parse_frontmatter, sanitize_filename

from conftest import FakeRuntime, make_card, rate_limit_error

SAMPLE_RESOURCES = Path(__file__).resolve().parents[1] / "examples" / "sample-vault" / "The Brain" / "3 - Resources"
LONG_CAPTION = "Weeknight garlic noodles with butter, chili, and scallions on the side."


def _meta(title="Neon drive", description=LONG_CAPTION, **extra) -> dict:
    base = {
        "url": "https://example.com/watch",
        "title": title,
        "description": description,
        "tags": ["synth"],
        "creator": "Rover",
        "artist": "Rover",
        "track": "Neon",
        "album": "",
    }
    return {**base, **extra}


def _analysis(**overrides) -> str:
    payload = {
        "category": "Music",
        "summary": "Night drive synth to queue this week.",
        "actionable_items": ["Rover", "Neon"],
        "confidence": 0.91,
        "needs_manual_review": False,
        "artist": "Rover",
        "sound": "Neon",
    }
    payload.update(overrides)
    return json.dumps(payload)


def _queue(settings, *lines: str) -> Path:
    inbox = settings.model_c_inbox_path
    inbox.parent.mkdir(parents=True, exist_ok=True)
    inbox.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
    return inbox


def _patch_fetch(monkeypatch, meta):
    monkeypatch.setattr("second_brain.model_c.ingest.fetch_model_c_metadata", lambda *args, **kwargs: meta)


def test_transcript_needs_vision_for_short_and_instrumental_text():
    assert transcript_needs_vision(None)
    assert transcript_needs_vision("pure music, no speech here at all")
    assert not transcript_needs_vision("Squat down for three sets of ten, then rest.")


def test_music_note_is_inbox_and_keeps_sound_fields(settings, monkeypatch):
    inbox = _queue(settings, "https://example.com/watch")
    _patch_fetch(monkeypatch, _meta(title='bad\\name/*?:"<>| track'))
    result = run_ingest(settings, runtime=FakeRuntime([_analysis()]))
    assert result.created == 1 and result.stopped_reason is None
    assert inbox.read_text(encoding="utf-8") == ""

    notes = list((settings.resources_dir / "Music").glob("*.md"))
    card = next(path for path in notes if path.name != "Music Hub.md")
    assert "[" not in card.stem and "*" not in card.stem and "?" not in card.stem
    fm = parse_frontmatter(card.read_text(encoding="utf-8"))
    assert fm["status"] == "inbox"
    assert fm["category"] == "Music"
    assert fm["sound"] == "Neon"
    assert fm["artist"] == "Rover"
    assert fm["confidence"] == 0.91
    assert "second-brain" in fm["tags"]
    assert "category/music" in fm["tags"]
    assert "## Artist & Sound" in card.read_text(encoding="utf-8")
    assert UrlLedger(settings.ledger_path).has_event("https://example.com/watch", "carded")


def test_fitness_and_recipe_notes_use_checkboxes_and_ready(settings, monkeypatch):
    _queue(settings, "https://example.com/fit", "https://example.com/food")
    metas = iter([
        _meta(title="Push session", description=LONG_CAPTION, creator="coach"),
        _meta(title="Garlic noodles", description=LONG_CAPTION, creator="kitchen", artist="", track=""),
    ])
    monkeypatch.setattr("second_brain.model_c.ingest.fetch_model_c_metadata", lambda *args, **kwargs: next(metas))
    runtime = FakeRuntime([
        _analysis(
            category="Fitness & Health",
            summary="Push workout for a short session.",
            actionable_items=["Push-up 3x10", "Plank 45s"],
            artist=None,
            sound=None,
        ),
        _analysis(
            category="Recipes & Food",
            summary="Fifteen minute garlic noodles.",
            actionable_items=["200g noodles", "Boil, then toss with garlic butter"],
            artist=None,
            sound=None,
        ),
    ])
    result = run_ingest(settings, runtime=runtime)
    assert result.created == 2

    fit = next((settings.resources_dir / "Fitness & Health").glob("Push*.md"))
    fit_text = fit.read_text(encoding="utf-8")
    assert parse_frontmatter(fit_text)["status"] == "ready"
    assert "## Exercises & Movement Routine" in fit_text
    assert "- [ ] Push-up 3x10" in fit_text

    food = next((settings.resources_dir / "Recipes & Food").glob("Garlic*.md"))
    food_text = food.read_text(encoding="utf-8")
    assert parse_frontmatter(food_text)["status"] == "ready"
    assert "## Ingredients & Preparation" in food_text
    assert "- [ ] 200g noodles" in food_text


def test_low_confidence_and_ambiguous_go_to_manual_review(settings, monkeypatch):
    _queue(settings, "https://example.com/low", "https://example.com/other")
    metas = iter([_meta(title="Unclear clip"), _meta(title="Something else")])
    monkeypatch.setattr("second_brain.model_c.ingest.fetch_model_c_metadata", lambda *args, **kwargs: next(metas))
    runtime = FakeRuntime([
        _analysis(confidence=0.59),
        _analysis(category="Other / Ambiguous", confidence=0.95, summary="Not one of the three."),
    ])
    run_ingest(settings, runtime=runtime)
    review = settings.resources_dir / "Inbox" / "Manual Review"
    cards = [path for path in review.glob("*.md")]
    assert len(cards) == 2
    for path in cards:
        fm = parse_frontmatter(path.read_text(encoding="utf-8"))
        assert fm["status"] == "manual-review"
        assert fm["category"] == "Ambiguous"


def test_dead_link_skips_the_model(settings, monkeypatch):
    inbox = _queue(settings, "https://example.com/gone")
    monkeypatch.setattr("second_brain.model_c.ingest.fetch_model_c_metadata", lambda *args, **kwargs: None)
    runtime = FakeRuntime([])
    result = run_ingest(settings, runtime=runtime)
    assert result.created == 1
    assert runtime.calls == []
    assert inbox.read_text(encoding="utf-8") == ""
    card = next((settings.resources_dir / "Inbox" / "Manual Review").glob("*.md"))
    text = card.read_text(encoding="utf-8")
    fm = parse_frontmatter(text)
    assert fm["status"] == "manual-review"
    assert fm["confidence"] == 0.0
    assert "https://example.com/gone" in text
    assert "No model call was made." in text


def test_known_url_is_popped_without_a_model_call(settings):
    make_card(settings.resources_dir, "Music", "Already saved", "https://example.com/known")
    inbox = _queue(settings, "https://example.com/known?utm_source=share")
    runtime = FakeRuntime([])
    result = run_ingest(settings, runtime=runtime)
    assert result.skipped_known == 1 and result.created == 0
    assert runtime.calls == []
    assert inbox.read_text(encoding="utf-8") == ""


def test_rate_limit_leaves_the_url_in_the_queue(settings, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda _seconds: None)
    inbox = _queue(settings, "https://example.com/later")
    _patch_fetch(monkeypatch, _meta())
    result = run_ingest(settings, runtime=FakeRuntime([rate_limit_error() for _ in range(5)]))
    assert result.created == 0 and result.stopped_reason
    assert "https://example.com/later" in inbox.read_text(encoding="utf-8")
    assert not list((settings.resources_dir / "Music").glob("Neon*.md"))


def test_sparse_caption_calls_audio_then_keyframes(settings, monkeypatch):
    _queue(settings, "https://example.com/sparse")
    _patch_fetch(monkeypatch, _meta(title="Hi", description="", tags=[]))
    calls = {"audio": 0, "frames": 0}

    def audio(*_args, **_kwargs):
        calls["audio"] += 1
        return ""

    def frames(*_args, **_kwargs):
        calls["frames"] += 1
        return []

    monkeypatch.setattr("second_brain.model_c.ingest.extract_audio_transcript", audio)
    monkeypatch.setattr("second_brain.model_c.ingest.extract_keyframes", frames)
    run_ingest(settings, runtime=FakeRuntime([_analysis()]))
    assert calls == {"audio": 1, "frames": 1}


def test_long_caption_skips_audio_and_keyframes(settings, monkeypatch):
    _queue(settings, "https://example.com/rich")
    _patch_fetch(monkeypatch, _meta())
    calls = {"audio": 0, "frames": 0}
    monkeypatch.setattr(
        "second_brain.model_c.ingest.extract_audio_transcript",
        lambda *_a, **_k: calls.__setitem__("audio", calls["audio"] + 1),
    )
    monkeypatch.setattr(
        "second_brain.model_c.ingest.extract_keyframes",
        lambda *_a, **_k: calls.__setitem__("frames", calls["frames"] + 1),
    )
    run_ingest(settings, runtime=FakeRuntime([_analysis()]))
    assert calls == {"audio": 0, "frames": 0}


def test_filename_sanitizer_strips_illegal_characters():
    assert sanitize_filename('a\\b/c*d?e:f"g<h>i|j', max_len=50) == "abcdefghij"


def test_hubs_match_sample_vault_and_are_not_overwritten(settings):
    assert "file.ctime >= date(today) - dur(7 days)" in MUSIC_HUB
    assert 'status = "ready"' in FITNESS_HUB
    assert 'status = "ready"' in RECIPES_HUB
    assert 'elink(url, "Open")' in MUSIC_HUB
    for folder, (name, body) in HUBS.items():
        sample = (SAMPLE_RESOURCES / folder / name).read_text(encoding="utf-8")
        assert sample == body

    ensure_hubs(settings.resources_dir)
    hub = settings.resources_dir / "Music" / "Music Hub.md"
    assert hub.read_text(encoding="utf-8") == MUSIC_HUB
    hub.write_text("# edited\n", encoding="utf-8")
    ensure_hubs(settings.resources_dir)
    assert hub.read_text(encoding="utf-8") == "# edited\n"


def test_render_note_does_not_use_the_summary_as_a_path():
    analysis = ModelCAnalysis.model_validate_json(_analysis())
    text = render_note(_meta(), analysis, "https://example.com/watch", "inbox", "Music")
    assert "category: \"Music\"" in text
    assert "../" not in text.split("---", 2)[1]


def test_model_c_config_defaults_and_rejects_a_bad_threshold(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    settings = settings_from_dict({"vault_path": str(vault), "data_dir": str(tmp_path / "data")}, base_dir=tmp_path)
    assert settings.model_c.confidence_threshold == 0.60
    assert settings.model_c.sparse_text_chars == 45
    assert settings.model_c_inbox_path == vault / "3 - Resources" / "Inbox" / "model_c_pending.txt"
    with pytest.raises(ConfigError):
        settings_from_dict(
            {"vault_path": str(vault), "data_dir": str(tmp_path / "data"), "model_c": {"confidence_threshold": 1.5}},
            base_dir=tmp_path,
        )
