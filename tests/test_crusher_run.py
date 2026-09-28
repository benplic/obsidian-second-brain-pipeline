"""Crusher orchestration (mocked acquisition + Gemini)."""

from __future__ import annotations

import pytest

from second_brain.crusher import CrushOptions, run_crush
from second_brain.crusher.acquire import TIER_CAPTIONS, TIER_VISUAL, AcquiredMedia
from second_brain.crusher.budget import SpendCapReachedError
from second_brain.crusher.schema import CrusherAnalysis, ExtractedItem
from second_brain.crusher.state import STATUS_FAILED_RETRYABLE, STATUS_UNAVAILABLE, CrusherState
from second_brain.crusher.ytdlp import TransientFetchError

from conftest import make_card

CRUSHER_MARKER = "<!-- crusher:start -->"
WORDS = " ".join(["speech"] * 80)


@pytest.fixture
def crush_settings(settings, monkeypatch):
    monkeypatch.setattr("second_brain.crusher.run.ensure_ffmpeg", lambda: None)
    monkeypatch.setattr("second_brain.crusher.run.run_postpass", lambda settings: (0, 0))
    monkeypatch.setattr(
        "second_brain.crusher.budget.BudgetManager.create_client",
        lambda self: (object(), "GEMINI_API_KEY"),
    )
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    return settings


def _text_media(url: str) -> AcquiredMedia:
    return AcquiredMedia(
        url=url, title="T", description="D", transcript_text=WORDS, transcript_source="captions",
        tier_reached=TIER_CAPTIONS,
    )


def _analysis(**kw) -> CrusherAnalysis:
    base = dict(category="Tech & Coding", confidence=0.9, title="Python tip", summary="Dedupe lists", recategorize=True)
    base.update(kw)
    return CrusherAnalysis(**base)


def test_crush_dry_run_then_apply_from_cache(crush_settings, monkeypatch):
    url = "https://www.tiktok.com/@u/video/8001"
    card = make_card(crush_settings.resources_dir, "Inbox", "dry", url)
    visuals: list[str] = []
    monkeypatch.setattr("second_brain.crusher.run.acquire_text", lambda u, cfg, runner, **kw: _text_media(u))
    monkeypatch.setattr("second_brain.crusher.run.acquire_visuals", lambda m, cfg, runner: visuals.append(m.url))
    monkeypatch.setattr("second_brain.crusher.run.analyze_media", lambda *a, **k: _analysis())

    result = run_crush(crush_settings, CrushOptions(apply=False, limit=1, reprocess=True))
    assert result.analyzed == 1
    assert visuals == []  # text-rich captions never touch the visual tier
    assert result.tiers["captions"] == 1
    assert len(list(crush_settings.crusher_cache_dir.glob("*.json"))) == 1
    assert CRUSHER_MARKER not in card.read_text(encoding="utf-8")

    apply_result = run_crush(crush_settings, CrushOptions(apply=True, limit=1))
    assert apply_result.written == 1 and apply_result.skipped == 1
    tech_card = crush_settings.resources_dir / "Tech & Coding" / "Python tip.md"
    assert tech_card.is_file()
    assert "Video Analysis" in tech_card.read_text(encoding="utf-8")


def test_short_text_list_escalates_to_keyframes(crush_settings, monkeypatch, tmp_path):
    url = "https://www.tiktok.com/@u/video/8002"
    make_card(crush_settings.resources_dir, "Inbox", "list", url)
    frame = tmp_path / "kf_001.jpg"
    frame.write_bytes(b"\xff\xd8")

    def fake_visuals(media, cfg, runner):
        media.tier_reached = TIER_VISUAL
        media.keyframe_paths = [frame]

    calls: list[int] = []

    def fake_analyze(budget, cfg, media, ctx, taxonomy):
        calls.append(len(media.keyframe_paths))
        count = 6 if not media.keyframe_paths else 10
        return _analysis(
            category="Saved Music", title="Top songs", list_expected_count=10,
            items=[ExtractedItem(kind="song", name=f"s{i}", position=i + 1) for i in range(count)],
        )

    monkeypatch.setattr("second_brain.crusher.run.acquire_text", lambda u, cfg, runner, **kw: _text_media(u))
    monkeypatch.setattr("second_brain.crusher.run.acquire_visuals", fake_visuals)
    monkeypatch.setattr("second_brain.crusher.run.analyze_media", fake_analyze)

    result = run_crush(crush_settings, CrushOptions(limit=1, reprocess=True))
    assert calls == [0, 1]  # text first, then one pass with frames
    assert result.tiers["visual"] == 1
    assert result.needs_review == 0


def test_spend_cap_stops_and_resume_skips_done(crush_settings, monkeypatch):
    urls = [f"https://www.tiktok.com/@u/video/90{i}" for i in range(3)]
    for i, u in enumerate(urls):
        make_card(crush_settings.resources_dir, "Inbox", f"c{i}", u)
    monkeypatch.setattr("second_brain.crusher.run.acquire_text", lambda u, cfg, runner, **kw: _text_media(u))
    seen: list[str] = []

    def capped(budget, cfg, media, ctx, taxonomy):
        if len(seen) >= 1:
            raise SpendCapReachedError("cap")
        seen.append(media.url)
        return _analysis(title=f"t{len(seen)}")

    monkeypatch.setattr("second_brain.crusher.run.analyze_media", capped)
    first = run_crush(crush_settings, CrushOptions())
    assert first.analyzed == 1 and first.stopped_reason == "cap"

    monkeypatch.setattr("second_brain.crusher.run.analyze_media", lambda *a, **k: _analysis(title="later"))
    second = run_crush(crush_settings, CrushOptions())
    assert second.skipped == 1 and second.analyzed == 2


def test_transient_fetch_is_retryable_then_unavailable(crush_settings, monkeypatch):
    url = "https://www.tiktok.com/@u/video/7777"
    make_card(crush_settings.resources_dir, "Inbox", "flaky", url)

    def boom(u, cfg, runner, **kw):
        raise TransientFetchError("HTTP Error 429")

    monkeypatch.setattr("second_brain.crusher.run.acquire_text", boom)
    state_of = lambda: CrusherState(crush_settings.crusher_state_path, crush_settings.crusher_cache_dir).get(url)

    for _ in range(crush_settings.crusher.unavailable_after_attempts - 1):
        result = run_crush(crush_settings, CrushOptions())
        assert result.retryable == 1
        assert state_of().status == STATUS_FAILED_RETRYABLE
    final = run_crush(crush_settings, CrushOptions())
    assert final.unavailable == 1
    assert state_of().status == STATUS_UNAVAILABLE


def test_max_spend_flag_overrides_config(crush_settings, monkeypatch):
    make_card(crush_settings.resources_dir, "Inbox", "x", "https://www.tiktok.com/@u/video/5")
    seen: list = []
    monkeypatch.setattr("second_brain.crusher.run.acquire_text", lambda u, cfg, runner, **kw: _text_media(u))

    def spy(budget, cfg, media, ctx, taxonomy):
        seen.append(cfg.max_spend_usd)
        return _analysis()

    monkeypatch.setattr("second_brain.crusher.run.analyze_media", spy)
    run_crush(crush_settings, CrushOptions(max_spend=1.25))
    assert seen == [1.25]
