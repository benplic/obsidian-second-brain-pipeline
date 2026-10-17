"""Model B helpers used by the shared dispatcher.

Tier 1 metadata is already on ``meta`` (yt-dlp JSON, no download). Sparse
captions sample keyframes before any audio call. Spoken clips with no place
name fall back to low-bitrate audio. ``run_ingest`` reads ``pending_links.txt``
through the dispatcher and does not pop a second queue.
"""

from __future__ import annotations

import logging
import tempfile

from ..config import Settings
from ..llm.runtime import LlmRuntime
from ..llm.types import CompletionRequest
from ..model_a.ingest import extract_audio_transcript
from ..model_c.ingest import extract_keyframes
from .schema import TravelExtraction, place_query

logger = logging.getLogger(__name__)

# Travel slideshows hide the city name mid-clip and near the end card.
TRAVEL_KEYFRAME_FRACTIONS = (0.25, 0.55, 0.85)


def caption_is_sparse(description: str | None, limit: int) -> bool:
    """True when the caption is too short to name a place on its own."""
    return len((description or "").strip()) < limit


def _needs_audio(extraction: TravelExtraction) -> bool:
    """Spoken vlog: travel, but the frames and caption never named a place."""
    return extraction.is_travel_related and place_query(extraction) is None


def _prompt(meta: dict, transcript: str | None) -> str:
    tags = meta.get("tags") or []
    return (
        "Extract a travel place from this saved video for an Obsidian map.\n"
        "Set is_travel_related=false when the clip is not about a place, trip, restaurant, landmark, or region.\n"
        "place_name is a city, landmark, restaurant, or region. Leave it null when you cannot name one.\n"
        "summary is 5-10 words of highlights or tips.\n"
        "confidence is 0.0 to 1.0.\n"
        f"Title: {meta.get('title') or ''}\n"
        f"Caption: {meta.get('description') or ''}\n"
        f"Tags: {', '.join(str(tag) for tag in tags)}\n"
        f"Creator: {meta.get('creator') or 'Unknown'}\n"
        f"Audio transcript: {transcript or 'N/A'}\n"
    )


def _complete(
    runtime: LlmRuntime,
    meta: dict,
    frames: list,
    transcript: str | None,
) -> TravelExtraction:
    result = runtime.complete(CompletionRequest(
        prompt=_prompt(meta, transcript),
        response_schema=TravelExtraction,
        extra_image_objects=frames,
    ))
    return TravelExtraction.model_validate_json(result.text or "")


def analyze_travel(runtime: LlmRuntime, settings: Settings, meta: dict, url: str) -> TravelExtraction:
    """Tier 3 before tier 2 when the caption is sparse. Rate limits propagate."""
    caption = str(meta.get("description") or "")
    if caption_is_sparse(caption, settings.model_b.sparse_text_chars):
        logger.info("  - Sparse travel caption. Sampling keyframes (tier 3)...")
        with tempfile.TemporaryDirectory() as tmp:
            frames = extract_keyframes(url, tmp, fractions=TRAVEL_KEYFRAME_FRACTIONS)
            extraction = _complete(runtime, meta, frames, None)
            if not _needs_audio(extraction):
                return extraction
            logger.info("  - No place in the frames. Trying audio (tier 2)...")
            transcript = extract_audio_transcript(runtime, settings, url, tmp)
            if not transcript:
                return extraction
            return _complete(runtime, meta, frames, transcript)

    extraction = _complete(runtime, meta, [], None)
    if not _needs_audio(extraction):
        return extraction
    logger.info("  - Travel clip has no place name. Trying audio (tier 2)...")
    with tempfile.TemporaryDirectory() as tmp:
        transcript = extract_audio_transcript(runtime, settings, url, tmp)
    if not transcript:
        return extraction
    return _complete(runtime, meta, [], transcript)


def run_ingest(settings: Settings, runtime: LlmRuntime | None = None):
    """Read the shared pending-links queue."""
    from ..dispatch import run_dispatch

    return run_dispatch(settings, runtime)
