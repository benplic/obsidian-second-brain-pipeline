"""Shared queue: classify, then transform. No network."""

from __future__ import annotations

from second_brain.crusher.classify import ClassificationResult
from second_brain.crusher.ytdlp import PermanentMediaError, RunResult, TransientFetchError
from second_brain.dispatch import acquire_metadata, run_dispatch
from second_brain.ledger import UrlLedger
from second_brain.model_c.hubs import FITNESS_HUB, MUSIC_HUB, RECIPES_HUB
from second_brain.model_c.schema import ModelCExtraction
from second_brain.steps.write_cards import render_card
from second_brain.taxonomy import category_tag, folder_for
from second_brain.taxonomy import DEFAULT_FOLDER_MAP
from second_brain.vault import parse_frontmatter

from conftest import FakeRuntime, make_card, rate_limit_error


class _Jev:
    name = "jev"

    def __init__(self, category: str, confidence: float):
        self.category = category
        self.confidence = confidence
        self.calls = 0

    def classify(self, state: str, *, taxonomy: list[str]):
        self.calls += 1
        return ClassificationResult(category=self.category, category_confidence=self.confidence)


def _queue(settings, *lines: str):
    path = settings.pending_links_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
    return path


def _meta(title="Neon drive", description="A long caption about a night drive synth track worth saving."):
    return {
        "url": "https://example.com/watch",
        "title": title,
        "description": description,
        "tags": ["synth"],
        "creator": "Rover",
        "artist": "Rover",
        "track": "Neon",
        "transcript": "",
    }


def _extraction(**overrides) -> str:
    payload = {
        "summary": "Night drive synth to queue this week.",
        "actionable_items": ["Rover", "Neon"],
        "confidence": 0.91,
        "needs_manual_review": False,
        "artist": "Rover",
        "sound": "Neon",
    }
    payload.update(overrides)
    import json
    return json.dumps(payload)


def test_category_tag_is_obsidian_legal():
    assert category_tag("Recipes & Food") == "category/recipes-and-food"
    assert category_tag("Art/Organization/Spaces") == "category/art-organization-spaces"
    assert category_tag("Music") == "category/saved-music"
    assert folder_for("Music", DEFAULT_FOLDER_MAP) == "Music"


def test_saved_music_does_not_ask_the_model_for_a_category(settings):
    inbox = _queue(settings, "https://example.com/watch")
    runtime = FakeRuntime([_extraction()])
    result = run_dispatch(
        settings,
        runtime,
        acquire=lambda url: _meta(),
        classifier=_Jev("Saved Music", 0.95),
    )
    assert result.created == 1 and result.stopped_reason is None
    assert inbox.read_text(encoding="utf-8") == ""
    request = runtime.calls[0]
    assert request.response_schema is ModelCExtraction
    assert "category" not in ModelCExtraction.model_fields
    card = next(path for path in (settings.resources_dir / "Music").glob("*.md") if "Hub" not in path.name)
    fm = parse_frontmatter(card.read_text(encoding="utf-8"))
    assert fm["category"] == "Saved Music"
    assert fm["status"] == "inbox"
    assert "stage" not in fm
    assert fm["sound"] == "Neon"
    assert "saved-media" in fm["tags"]
    assert "category/saved-music" in fm["tags"]


def test_low_jev_confidence_skips_the_transform(settings):
    _queue(settings, "https://example.com/watch")
    runtime = FakeRuntime([])
    result = run_dispatch(
        settings,
        runtime,
        acquire=lambda url: _meta(),
        classifier=_Jev("Saved Music", 0.5),
    )
    assert result.created == 1
    assert runtime.calls == []
    card = next((settings.resources_dir / "Inbox" / "Manual Review").glob("*.md"))
    fm = parse_frontmatter(card.read_text(encoding="utf-8"))
    assert fm["status"] == "inbox"
    assert fm["stage"] == "manual-review"


def test_weak_extraction_is_manual_review_and_popped(settings):
    inbox = _queue(settings, "https://example.com/watch")
    run_dispatch(
        settings,
        FakeRuntime([_extraction(confidence=0.59)]),
        acquire=lambda url: _meta(),
        classifier=_Jev("Recipes & Food", 0.9),
    )
    assert inbox.read_text(encoding="utf-8") == ""
    card = next((settings.resources_dir / "Inbox" / "Manual Review").glob("*.md"))
    fm = parse_frontmatter(card.read_text(encoding="utf-8"))
    assert fm["stage"] == "manual-review"
    assert fm["status"] == "inbox"


def test_transient_fetch_stays_in_the_queue(settings):
    inbox = _queue(settings, "https://example.com/later")

    def acquire(url):
        raise TransientFetchError("http error 429")

    result = run_dispatch(settings, FakeRuntime([]), acquire=acquire, classifier=_Jev("Saved Music", 0.9))
    assert result.created == 0
    assert "https://example.com/later" in inbox.read_text(encoding="utf-8")
    assert not UrlLedger(settings.ledger_path).urls


def test_permanent_fetch_is_manual_review(settings):
    inbox = _queue(settings, "https://example.com/gone")

    def acquire(url):
        raise PermanentMediaError("video unavailable")

    result = run_dispatch(settings, FakeRuntime([]), acquire=acquire, classifier=_Jev("Saved Music", 0.9))
    assert result.created == 1
    assert inbox.read_text(encoding="utf-8") == ""
    card = next((settings.resources_dir / "Inbox" / "Manual Review").glob("*.md"))
    fm = parse_frontmatter(card.read_text(encoding="utf-8"))
    assert fm["stage"] == "manual-review"
    assert fm["confidence"] == 0.0
    assert "https://example.com/gone" in card.read_text(encoding="utf-8")


def test_unparseable_response_continues(settings):
    inbox = _queue(settings, "https://example.com/bad", "https://example.com/good")
    metas = iter([_meta(title="Bad clip"), _meta(title="Good noodles", description="A long enough caption for a recipe video here.")])
    result = run_dispatch(
        settings,
        FakeRuntime(["this is not json", _extraction(summary="Boil noodles.", actionable_items=["noodles"], confidence=0.9)]),
        acquire=lambda url: next(metas),
        classifier=_Jev("Recipes & Food", 0.92),
    )
    assert result.stopped_reason is None and result.created == 2
    assert inbox.read_text(encoding="utf-8") == ""
    review = list((settings.resources_dir / "Inbox" / "Manual Review").glob("*.md"))
    recipes = [path for path in (settings.resources_dir / "Recipes & Food").glob("*.md") if "Hub" not in path.name]
    assert len(review) == 1 and len(recipes) == 1
    assert "- [ ] noodles" in recipes[0].read_text(encoding="utf-8")


def test_rate_limit_leaves_the_url(settings, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda _seconds: None)
    inbox = _queue(settings, "https://example.com/later")
    result = run_dispatch(
        settings,
        FakeRuntime([rate_limit_error() for _ in range(5)]),
        acquire=lambda url: _meta(),
        classifier=_Jev("Saved Music", 0.9),
    )
    assert result.created == 0 and result.stopped_reason
    assert "https://example.com/later" in inbox.read_text(encoding="utf-8")


def test_known_url_is_popped_without_acquire(settings):
    make_card(settings.resources_dir, "Music", "Already", "https://example.com/known")
    inbox = _queue(settings, "https://example.com/known")
    calls = {"n": 0}

    def acquire(url):
        calls["n"] += 1
        return _meta()

    result = run_dispatch(settings, FakeRuntime([]), acquire=acquire, classifier=_Jev("Saved Music", 0.9))
    assert result.skipped_known == 1 and result.created == 0
    assert calls["n"] == 0
    assert inbox.read_text(encoding="utf-8") == ""


def test_thin_text_requests_keyframes(settings, monkeypatch):
    _queue(settings, "https://example.com/sparse")
    calls = {"frames": 0}

    def frames(url, tmp):
        calls["frames"] += 1
        return []

    monkeypatch.setattr("second_brain.dispatch.extract_keyframes", frames)
    run_dispatch(
        settings,
        FakeRuntime([_extraction()]),
        acquire=lambda url: _meta(title="Hi", description=""),
        classifier=_Jev("Saved Music", 0.9),
    )
    assert calls["frames"] == 1


def test_mega_batch_recipe_row_renders_checkboxes():
    _, _, content = render_card({
        "Category": "Recipes & Food",
        "Summary": "Boil noodles",
        "Creator": "kitchen",
        "URL": "https://example.com/food",
        "Title": "Garlic noodles",
        "Tags": "",
    })
    fm = parse_frontmatter(content)
    assert fm["status"] == "inbox" and fm["stage"] == "ready"
    assert "## Ingredients & Preparation" in content
    assert "- [ ] Boil noodles" in content


def test_legacy_inbox_is_not_popped(settings):
    legacy = settings.model_c_inbox_path
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text("https://example.com/old\n", encoding="utf-8")
    _queue(settings, "https://example.com/new")
    run_dispatch(
        settings,
        FakeRuntime([_extraction()]),
        acquire=lambda url: _meta(),
        classifier=_Jev("Saved Music", 0.9),
    )
    assert legacy.read_text(encoding="utf-8") == "https://example.com/old\n"


def test_acquire_metadata_treats_timeout_as_transient():
    class Runner:
        def run(self, extra, url, timeout=None):
            return RunResult(returncode=1, stdout="", stderr="timed out after 20s")

    try:
        acquire_metadata(object(), "https://example.com/x", runner=Runner())  # type: ignore[arg-type]
    except TransientFetchError:
        return
    raise AssertionError("timeout should stay retryable")


def test_hubs_list_inbox_and_ready_notes():
    assert 'status != "tossed"' in FITNESS_HUB and 'status != "tossed"' in RECIPES_HUB
    assert 'stage != "archived"' in MUSIC_HUB
    assert "file.ctime >= date(today) - dur(7 days)" in MUSIC_HUB
    assert HUB_MARKER_PRESENT()


def HUB_MARKER_PRESENT() -> bool:
    return "second-brain-hub:1" in MUSIC_HUB
