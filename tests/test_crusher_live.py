"""Live smoke tests for crusher_cases.yaml (network + Gemini).

Run: pytest -m live tests/test_crusher_live.py -v
Requires GEMINI_API_KEY, ffmpeg, yt-dlp, and optional cookies for Instagram.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from second_brain.config import load_settings
from second_brain.crusher import CrushOptions, run_crush
from second_brain.crusher.state import CrusherState, STATUS_UNAVAILABLE
from second_brain.urls import normalize_url

from conftest import make_card

FIXTURES = Path(__file__).parent / "fixtures" / "crusher_cases.yaml"


def _load_cases() -> list[dict]:
    raw = yaml.safe_load(FIXTURES.read_text(encoding="utf-8")) or {}
    return list(raw.get("cases") or [])


@pytest.fixture(scope="module")
def live_settings(tmp_path_factory):
    vault = tmp_path_factory.mktemp("crusher_live_vault")
    (vault / "3 - Resources" / "Inbox").mkdir(parents=True)
    data = tmp_path_factory.mktemp("crusher_live_data")
    from second_brain.config import settings_from_dict

    return settings_from_dict(
        {
            "vault_path": str(vault),
            "data_dir": str(data),
            "crusher": {
                "model": "gemini-3.6-flash",
                "fallback_model": "gemini-3.8-flash",
                "requests_per_day": None,
                "requests_per_minute": None,
                "tokens_per_minute": None,
                "max_passes": 2,
            },
        },
        base_dir=tmp_path_factory.mktemp("cfg"),
    )


@pytest.mark.live
@pytest.mark.parametrize("case", _load_cases(), ids=[c["id"] for c in _load_cases()])
def test_crusher_live_case(case, live_settings, monkeypatch):
    monkeypatch.chdir(Path(__file__).resolve().parents[1])
    from second_brain.cli import _load_dotenv

    _load_dotenv()
    url = case["url"]
    if "@example/" in url:
        pytest.skip("Placeholder URL — replace with a real link in crusher_cases.yaml")

    make_card(
        live_settings.resources_dir,
        "Inbox",
        case["id"][:40],
        url,
    )
    result = run_crush(
        live_settings,
        CrushOptions(apply=False, url=url, reprocess=True, write_children=False),
    )
    if result.stopped_reason and "503" in (result.stopped_reason or ""):
        pytest.skip(f"Gemini unavailable: {result.stopped_reason}")
    assert not result.stopped_reason, result.stopped_reason

    state = CrusherState(live_settings.crusher_state_path, live_settings.crusher_cache_dir)
    norm = normalize_url(url)

    if case.get("expect_unavailable"):
        st = state.get(norm)
        assert st and st.status == STATUS_UNAVAILABLE
        return

    st = state.get(norm)
    if st and st.status == "failed":
        pytest.fail(f"Crusher analysis failed: {st.last_error}")
    if st and st.status == STATUS_UNAVAILABLE:
        if case.get("expect_unavailable"):
            return
        pytest.fail(f"Media unavailable: {st.last_error}")

    analysis = state.load_analysis(norm, prompt_version=live_settings.crusher.prompt_version)
    assert analysis is not None, "Expected cached analysis"

    if expected := case.get("expected_category"):
        assert analysis.category == expected, f"got {analysis.category!r}"
    if not_expected := case.get("expected_category_not"):
        assert analysis.category != not_expected

    min_items = case.get("min_items")
    if min_items is not None:
        assert len(analysis.items) >= min_items, analysis.items

    if kinds := case.get("item_kinds"):
        assert analysis.items, "expected extracted items"
        for kind in kinds:
            # Spoken "top N songs" lists are often tagged song even when case says album.
            alts = {kind}
            if kind == "album":
                alts.add("song")
            if kind == "song":
                alts.add("album")
            assert any(i.kind in alts for i in analysis.items), f"missing kind {kind} (got {[i.kind for i in analysis.items]})"

    if shape := case.get("media_shape"):
        assert analysis.media_shape == shape, analysis.media_shape
