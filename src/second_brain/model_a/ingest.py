"""Model A ingest: per-URL tiered analysis straight into the vault.

Tier 1: yt-dlp metadata. If the caption is sparse (< 40 chars):
Tier 2: low-bitrate audio -> Gemini transcript. If still empty:
Tier 3: two keyframes -> Gemini vision (needs the ``model-a`` extra).

Changes vs the original script:
  * dedupes against vault + ledger before spending any request;
  * pops each URL from the inbox right after its card is written (the
    original cleared the whole inbox only at the very end, so a crash replayed
    everything and an exception mid-run left duplicates);
  * 429 backoff via the shared helper; non-rate-limit errors stop the run.
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


def _pop_inbox_line(inbox: Path, line: str) -> None:
    """Remove one processed line, re-reading so concurrent appends survive."""
    lines = [ln.strip() for ln in inbox.read_text(encoding="utf-8").splitlines() if ln.strip()]
    lines = [ln for ln in lines if ln != line]
    atomic_write_text(inbox, "".join(f"{ln}\n" for ln in lines))


def run_ingest(settings: Settings, runtime: LlmRuntime | None = None) -> IngestResult:
    result = IngestResult()
    settings.require_vault()
    inbox = settings.model_a_inbox_path
    if not inbox.exists():
        logger.info("No pending links file at %s", inbox)
        return result
    lines = [ln.strip() for ln in inbox.read_text(encoding="utf-8").splitlines() if ln.strip()]
    if not lines:
        logger.info("Model A inbox is empty.")
        return result

    ledger = UrlLedger(settings.ledger_path)
    known = get_vault_urls(settings.resources_dir) | ledger.urls
    runtime = runtime or build_runtime(settings)
    logger.info("Processing %d link(s) for Model A...", len(lines))

    for idx, line in enumerate(lines, 1):
        if not is_valid_http_url(line):
            logger.warning("[%d/%d] Not an http(s) URL, left in inbox: %r", idx, len(lines), line)
            continue
        url = normalize_url(line)
        if url in known:
            result.skipped_known += 1
            _pop_inbox_line(inbox, line)
            continue
        logger.info("[%d/%d] Analyzing: %s", idx, len(lines), url)
        try:
            meta, analysis = analyze_video(runtime, settings, url)
        except (DailyQuotaExhaustedError, RateLimitExhaustedError) as exc:
            result.stopped_reason = str(exc)
        except (UnsupportedFeatureError, ValidationError) as exc:
            # TODO: decide whether to route unparseable items to Manual Review instead of stopping.
            result.stopped_reason = f"Unparseable Gemini response: {exc}"
        if result.stopped_reason:
            logger.error("Stopping Model A ingest: %s. Remaining links stay in the inbox.", result.stopped_reason)
            break

        r = route(settings.resources_dir, analysis.category, analysis.confidence,
                  analysis.needs_manual_review, settings.model_a.confidence_threshold)
        stem = sanitize_filename(meta["title"] or analysis.summary, max_len=50, fallback="Untitled Video")
        path = write_card(r.target_dir, stem, render_note(meta, analysis, url, r.status, r.category_label))
        ledger.record(url, EVENT_CARDED, category=r.category_label, source="model-a-ingest")
        known.add(url)
        _pop_inbox_line(inbox, line)
        result.created += 1
        logger.info("  -> Created %s [status: %s]", path.name, r.status)

    return result
