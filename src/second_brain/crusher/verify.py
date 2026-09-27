"""Completeness checks and retry hints."""

from __future__ import annotations

from dataclasses import dataclass

from .acquire import AcquiredMedia
from .schema import CrusherAnalysis


@dataclass
class CompletenessResult:
    complete: bool
    expected_count: int | None
    actual_count: int
    retry_hint: str | None = None


def _positions(items) -> list[int]:
    out: list[int] = []
    for item in items:
        if item.position is not None:
            try:
                out.append(int(item.position))
            except (TypeError, ValueError):
                pass
    return sorted(set(out))


def check_completeness(analysis: CrusherAnalysis, media: AcquiredMedia) -> CompletenessResult:
    actual = len(analysis.items)
    expected_candidates: list[int] = []

    if analysis.list_expected_count is not None and analysis.list_expected_count > 0:
        expected_candidates.append(analysis.list_expected_count)
    if media.probe.expected_slide_count:
        expected_candidates.append(media.probe.expected_slide_count)
    if media.carousel_image_paths:
        expected_candidates.append(len(media.carousel_image_paths))
    if analysis.evidence.slides_seen and analysis.evidence.slides_seen > actual:
        expected_candidates.append(analysis.evidence.slides_seen)

    expected = max(expected_candidates) if expected_candidates else None

    positions = _positions(analysis.items)
    gap_hint = None
    if positions and len(positions) >= 2:
        full = set(range(min(positions), max(positions) + 1))
        missing = sorted(full - set(positions))
        if missing:
            gap_hint = f"Missing list positions: {missing[:10]}"

    if expected is not None and actual < expected:
        hint = gap_hint or f"Expected at least {expected} items but got {actual}."
        return CompletenessResult(False, expected, actual, hint)

    if gap_hint:
        return CompletenessResult(False, expected, actual, gap_hint)

    return CompletenessResult(True, expected, actual, None)
