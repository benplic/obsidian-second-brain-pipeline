"""Model A helpers. ``run_ingest`` reads ``pending_links.txt`` through the dispatcher.

Tech, Project Ideas, and Movies are transformed only after classification.
The tier helpers below remain for sparse clips; they do not choose a folder.
"""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError

from ..config import Settings
from ..gemini import DailyQuotaExhaustedError, RateLimitExhaustedError
from ..llm.factory import build_runtime
from ..llm.runtime import LlmRuntime
from ..llm.types import CompletionRequest, UnsupportedFeatureError
from ..io_utils import atomic_write_text
from ..ledger import EVENT_CARDED, UrlLedger
from ..steps.extract_metadata import fetch_metadata
from ..urls import is_valid_http_url, normalize_url
from ..vault import get_vault_urls, sanitize_filename, write_card, yaml_str
from .common import MODEL_A_CATEGORIES, CategoryType, route

logger = logging.getLogger(__name__)

_CREATION_FLAGS = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
SPARSE_TEXT_CHARS = 40
MIN_TRANSCRIPT_CHARS = 20


class VideoAnalysis(BaseModel):
    category: CategoryType = Field(description="Target category for Model A.")
    summary: str = Field(description="5-10 word actionable summary of the core concept or film title.")
    confidence: float = Field(description="Confidence score from 0.0 to 1.0.")
    needs_manual_review: bool = Field(description="True if context remains too ambiguous to classify reliably.")


@dataclass
class IngestResult:
    created: int = 0
    skipped_known: int = 0
    stopped_reason: str | None = None


def extract_audio_transcript(runtime: LlmRuntime, settings: Settings, url: str, tmp_dir: str) -> str | None:
    """Tier 2. Returns None when audio is unavailable; rate limits propagate."""
    if not settings.llm.supports_audio:
        logger.info("  [Audio tier skipped] llm.supports_audio is false for this provider.")
        return None
    audio_path = os.path.join(tmp_dir, "audio.mp3")
    cmd = ["yt-dlp", "-x", "--audio-format", "mp3", "--audio-quality", "9", "-o", audio_path, "--", url]
    try:
        subprocess.run(cmd, capture_output=True, timeout=40, check=True, creationflags=_CREATION_FLAGS)
    except (subprocess.SubprocessError, OSError) as exc:
        logger.info("  [Audio tier skipped] %s", exc)
        return None
    if not os.path.exists(audio_path):
        return None
    with open(audio_path, "rb") as handle:
        audio_bytes = handle.read()
    request = CompletionRequest(
        prompt="Provide a brief 1-2 sentence transcript/summary of the spoken instructions or discussion in this clip.",
        audio=(audio_bytes, "audio/mp3"),
    )
    result = runtime.complete(request)
    return (result.text or "").strip() or None


def extract_keyframes(url: str, tmp_dir: str) -> list:
    """Tier 3: grab frames at 30% and 70% of the smallest stream."""
    try:
        import cv2
        from PIL import Image
    except ImportError:
        logger.warning("  [Keyframe tier skipped] install extras: pip install -e .[model-a]")
        return []
    video_path = os.path.join(tmp_dir, "sample.mp4")
    cmd = ["yt-dlp", "-f", "worst[ext=mp4]/worst", "-o", video_path, "--", url]
    try:
        subprocess.run(cmd, capture_output=True, timeout=40, check=True, creationflags=_CREATION_FLAGS)
    except (subprocess.SubprocessError, OSError) as exc:
        logger.info("  [Keyframe tier skipped] %s", exc)
        return []
    images = []
    if os.path.exists(video_path):
        cap = cv2.VideoCapture(video_path)
        try:
            total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
            if total > 10:
                for frame_num in (int(total * 0.3), int(total * 0.7)):
                    cap.set(cv2.CAP_PROP_POS_FRAMES, frame_num)
                    ok, frame = cap.read()
                    if ok:
                        images.append(Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)))
        finally:
            cap.release()
    return images


def analyze_video(runtime: LlmRuntime, settings: Settings, url: str) -> tuple[dict, VideoAnalysis]:
    meta = fetch_metadata(url, settings.extract.timeout_seconds, settings.extract.cookies_from_browser) or {
        "title": "", "description": "", "tags": [], "creator": "Unknown", "url": url,
    }
    combined = f"{meta['title']} {meta['description']} {' '.join(meta['tags'])}".strip()
    transcript, frames = None, []
    with tempfile.TemporaryDirectory() as tmp_dir:
        if len(combined) < SPARSE_TEXT_CHARS:
            logger.info("  - Sparse caption. Trying audio (tier 2)...")
            transcript = extract_audio_transcript(runtime, settings, url, tmp_dir)
            if not transcript or len(transcript) < MIN_TRANSCRIPT_CHARS:
                logger.info("  - Audio empty. Trying keyframes (tier 3)...")
                frames = extract_keyframes(url, tmp_dir)

        prompt = (
            "Analyze this saved video for an Obsidian Second Brain under Model A.\n"
            f"Target Categories: {MODEL_A_CATEGORIES}.\n"
            f"Title: {meta['title']}\n"
            f"Caption: {meta['description']}\n"
            f"Tags: {', '.join(meta['tags'])}\n"
            f"Audio Transcript: {transcript or 'N/A'}\n\n"
            "If this does not fit Tech, Project Ideas, or Movies, or if context is missing, flag needs_manual_review=True."
        )
        request = CompletionRequest(
            prompt=prompt,
            response_schema=VideoAnalysis,
            extra_image_objects=frames,
        )
        result = runtime.complete(request)
    return meta, VideoAnalysis.model_validate_json(result.text or "")


def render_note(meta: dict, analysis: VideoAnalysis, url: str, status: str, category_label: str) -> str:
    # Model A historically tagged "second-brain" (main pipeline: "saved-media").
    return f"""---
category: {yaml_str(category_label)}
creator: {yaml_str(meta['creator'])}
url: {yaml_str(url)}
status: {yaml_str(status)}
confidence: {analysis.confidence}
tags:
  - second-brain
  - category/{category_label.lower().replace(' ', '-')}
---

# {meta['title'] or analysis.summary}

> **Summary:** {analysis.summary}

- **Creator:** @{meta['creator']}
- **Source Link:** [{url}]({url})
- **Status:** `{status}`

## Actionable Notes
- [ ] Review core concept
- 
"""


def run_ingest(settings: Settings, runtime: LlmRuntime | None = None) -> IngestResult:
    """Read pending_links.txt via the shared dispatcher. The Model A inbox is not popped."""
    from ..dispatch import run_dispatch

    dispatched = run_dispatch(settings, runtime)
    return IngestResult(created=dispatched.created, skipped_known=dispatched.skipped_known, stopped_reason=dispatched.stopped_reason)
