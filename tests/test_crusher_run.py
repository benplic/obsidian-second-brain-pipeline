"""Crusher orchestration (mocked media + Gemini)."""

from __future__ import annotations

import pytest

from second_brain.crusher import CrushOptions, run_crush
from second_brain.crusher.acquire import AcquiredMedia
from second_brain.crusher.schema import CrusherAnalysis

from conftest import FakeClient, make_card


@pytest.fixture
def crush_settings(settings, monkeypatch):
    monkeypatch.setattr("second_brain.crusher.run.ensure_ffmpeg", lambda: None)
    return settings


def test_crush_dry_run_then_apply_from_cache(crush_settings, monkeypatch):
    url = "https://www.tiktok.com/@u/video/8001"
    card = make_card(crush_settings.resources_dir, "Inbox", "dry", url)
    media = AcquiredMedia(url=url, title="T", description="D", video_path=None)
    media.carousel_image_paths = []  # noqa: would be list - use probe only path
    # Satisfy acquire path with a fake video file reference without download.
    from pathlib import Path

    fake = crush_settings.data_dir / "fake.mp4"
    fake.parent.mkdir(parents=True, exist_ok=True)
    fake.write_bytes(b"\x00")
    media.video_path = fake

    analysis = CrusherAnalysis(
        category="Tech & Coding",
        confidence=0.9,
        title="Python tip",
        summary="Dedupe lists with set",
        recategorize=True,
    )
    client = FakeClient([analysis.model_dump_json()])

    monkeypatch.setattr("second_brain.crusher.run.acquire_media", lambda u, cfg: media)
    monkeypatch.setattr("second_brain.crusher.run.analyze_media", lambda *args, **kwargs: analysis)
    monkeypatch.setattr(
        "second_brain.crusher.budget.BudgetManager.create_client",
        lambda self: (client, "GEMINI_API_KEY"),
    )
    monkeypatch.setattr("second_brain.crusher.run.run_postpass", lambda settings: (0, 0))

    result = run_crush(crush_settings, CrushOptions(apply=False, limit=1, reprocess=True))
    assert result.analyzed == 1
    assert len(list(crush_settings.crusher_cache_dir.glob("*.json"))) == 1
    before = card.read_text(encoding="utf-8")
    assert CRUSHER_MARKER not in before

    apply_result = run_crush(crush_settings, CrushOptions(apply=True, limit=1))
    assert apply_result.written == 1
    tech_card = crush_settings.resources_dir / "Tech & Coding" / "Python tip.md"
    assert tech_card.is_file()
    assert "Video Analysis" in tech_card.read_text(encoding="utf-8")


CRUSHER_MARKER = "<!-- crusher:start -->"
