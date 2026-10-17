"""Model C text gate. Queue behavior lives in test_dispatch.py."""

from __future__ import annotations

from second_brain.model_c.ingest import transcript_needs_vision


def test_transcript_needs_vision_for_short_and_instrumental_text():
    assert transcript_needs_vision(None)
    assert transcript_needs_vision("pure music, no speech here at all")
    assert not transcript_needs_vision("Squat down for three sets of ten, then rest.")
