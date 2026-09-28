"""Crusher state and cache."""

from second_brain.crusher.schema import CrusherAnalysis
from second_brain.crusher.state import STATUS_DONE, CrusherState


def test_state_skip_after_done(tmp_path):
    state_path = tmp_path / "state.jsonl"
    cache_dir = tmp_path / "cache"
    state = CrusherState(state_path, cache_dir)
    url = "https://www.tiktok.com/@u/video/1"
    analysis = CrusherAnalysis(
        category="Travel",
        confidence=0.7,
        title="T",
        summary="S",
    )
    state.save_analysis(url, analysis, prompt_version="1", passes=1)
    state.record(url, status=STATUS_DONE, prompt_version="1")
    assert state.should_skip(url, prompt_version="1", reprocess=False)
    assert not state.should_skip(url, prompt_version="1", reprocess=True)
