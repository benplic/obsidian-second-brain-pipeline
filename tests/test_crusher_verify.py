"""Crusher completeness verification."""

from second_brain.crusher.acquire import AcquiredMedia
from second_brain.crusher.probe import ProbeResult
from second_brain.crusher.schema import CrusherAnalysis, EvidenceFlags, ExtractedItem
from second_brain.crusher.verify import check_completeness


def test_completeness_detects_missing_items():
    analysis = CrusherAnalysis(
        category="Saved Music",
        confidence=0.9,
        title="Top albums",
        summary="Ten albums",
        list_expected_count=10,
        items=[ExtractedItem(kind="album", name=f"A{i}", position=i) for i in range(1, 8)],
        evidence=EvidenceFlags(slides_seen=10),
    )
    media = AcquiredMedia(url="https://example.com/v/1", carousel_image_paths=[])
    result = check_completeness(analysis, media)
    assert not result.complete
    assert result.retry_hint


def test_completeness_ok_when_counts_match():
    analysis = CrusherAnalysis(
        category="Travel",
        confidence=0.8,
        title="Slides",
        summary="Three cities",
        items=[
            ExtractedItem(kind="destination", name="A", position=1),
            ExtractedItem(kind="destination", name="B", position=2),
            ExtractedItem(kind="destination", name="C", position=3),
        ],
    )
    media = AcquiredMedia(
        url="https://example.com/v/2",
        probe=ProbeResult(expected_slide_count=3),
    )
    assert check_completeness(analysis, media).complete
