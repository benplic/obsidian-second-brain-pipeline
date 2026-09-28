"""Decide whether an item needs the visual tier (T3).

This is where the budget is won or lost. Text-only is roughly 10x cheaper
than text+frames, but skipping visuals on the wrong item silently drops your
edge cases: silent travel slideshows, "my favorite songs" lists shown only on
screen, and image carousels. The rules therefore lean toward escalating
whenever the text evidence is thin.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..config import CrusherSettings
from .acquire import AcquiredMedia
from .schema import CrusherAnalysis

# "top 10", "5 best", "my favorite albums", "ranking", "tier list" in caption/title.
_LIST_CLAIM_RE = re.compile(
    r"\b(top|best|favou?rite|my)\s+\d{1,2}\b"
    r"|\b\d{1,2}\s+(best|places|things|songs|albums|books|movies|shows|destinations|spots|tips|ways|"
    r"foods|restaurants|products|apps|tools|hacks|cities|countries|beaches|recipes)\b"
    r"|\bfavou?rite\s+(songs|albums|books|movies|places|artists|records)\b"
    r"|\branking\b|\btier\s+list\b",
    re.IGNORECASE,
)
_SPOKEN_LIST_MIN_WORDS = 120


@dataclass
class GateDecision:
    needs_visuals: bool
    reasons: list[str] = field(default_factory=list)


def has_list_claim(text: str) -> bool:
    return bool(_LIST_CLAIM_RE.search(text or ""))


def decide_visuals(
    media: AcquiredMedia,
    settings: CrusherSettings,
    *,
    analysis: CrusherAnalysis | None = None,
    retry_hint: str | None = None,
    jev_needs_visuals: float | None = None,
) -> GateDecision:
    """Deterministic rules first; Jev (or the text summary's own flag) breaks ties.

    Called twice per item: before the first summary (``analysis`` is None) and
    after it, so a text-only summary that finds 6 of a claimed 10 items
    escalates to frames.
    """
    reasons: list[str] = []
    if media.is_carousel or media.is_photo_post:
        reasons.append("carousel/photo post")
    if media.transcript_words < settings.min_transcript_words:
        reasons.append(f"transcript {media.transcript_words} words < {settings.min_transcript_words}")
    if media.speech_ratio is not None and media.speech_ratio < settings.min_speech_ratio:
        reasons.append(f"speech ratio {media.speech_ratio:.2f} < {settings.min_speech_ratio}")
    if retry_hint:
        reasons.append(f"completeness retry: {retry_hint}")

    if analysis is not None:
        expected = analysis.list_expected_count or 0
        if expected and len(analysis.items) < expected:
            reasons.append(f"list claims {expected} items, text found {len(analysis.items)}")
        if getattr(analysis, "needs_visuals", False):
            reasons.append("summarizer flagged on-screen-only content")
    elif (
        has_list_claim(f"{media.title} {media.description}")
        and media.transcript_source != "captions"
        and media.transcript_words < _SPOKEN_LIST_MIN_WORDS
    ):
        # A list claim with little speech is the "songs listed on screen" case.
        # A long spoken transcript ("my favorite albums" talking head) usually
        # names the items, so it stays text-only unless the summary comes up short.
        reasons.append("list claim with little speech")

    if jev_needs_visuals is not None and jev_needs_visuals >= settings.jev_needs_visuals_threshold:
        reasons.append(f"jev needs_visuals={jev_needs_visuals:.2f}")

    return GateDecision(needs_visuals=bool(reasons), reasons=reasons)
