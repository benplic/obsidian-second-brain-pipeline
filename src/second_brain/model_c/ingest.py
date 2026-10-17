"""Model C ingest: per-URL tiered analysis into Music, Fitness, or Recipes.

Tier 1: yt-dlp metadata (title, caption, tags, creator, artist, track).
If that text is shorter than ``model_c.sparse_text_chars`` (default 45):
Tier 2: low-bitrate audio transcribed by the configured LLM.
If the transcript is empty, very short, or only describes instrumental music:
Tier 3: three low-res keyframes for on-screen text (needs the ``model-a`` extra).

Dead links are filed under Manual Review without a model call. Known URLs
(vault frontmatter or the ledger) are popped from the queue and skipped.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
from dataclasses import dataclass

from pydantic import ValidationError

from ..config import Settings
from ..gemini import DailyQuotaExhaustedError, RateLimitExhaustedError
from ..io_utils import atomic_write_text
from ..ledger import EVENT_CARDED, UrlLedger
from ..llm.factory import build_runtime
from ..llm.runtime import LlmRuntime
from ..llm.types import CompletionRequest, UnsupportedFeatureError
from ..steps.extract_metadata import build_ytdlp_command
from ..taxonomy import category_tag
from ..urls import is_valid_http_url, normalize_url
from ..vault import get_vault_urls, sanitize_filename, write_card, yaml_str
from .common import route
from .hubs import ensure_hubs
from .schema import MODEL_C_CATEGORIES, ModelCAnalysis

logger = logging.getLogger(__name__)

_CREATION_FLAGS = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
MIN_TRANSCRIPT_CHARS = 20
KEYFRAME_FRACTIONS = (0.25, 0.50, 0.75)
KEYFRAME_MAX_PX = 512

# Phrases a transcript uses when the clip has no spoken instructions.
# Short transcripts escalate to keyframes for the same reason: on-screen lists.
_MUSIC_ONLY_MARKERS = (
    "instrumental",
    "no speech",
    "no spoken",
    "pure music",
    "music only",
    "just music",
)


@dataclass
class IngestResult:
    created: int = 0
    skipped_known: int = 0
    stopped_reason: str | None = None


def transcript_needs_vision(text: str | None) -> bool:
    """True when audio did not yield spoken instructions worth trusting alone."""
    cleaned = (text or "").strip()
    if len(cleaned) < MIN_TRANSCRIPT_CHARS:
        return True
    lowered = cleaned.lower()
    return any(marker in lowered for marker in _MUSIC_ONLY_MARKERS)


def fetch_model_c_metadata(url: str, timeout_seconds: int, cookies_from_browser: str | None) -> dict | None:
    """Tier 1 metadata, including artist and track, or None when the link is dead.

    Step 1's ``fetch_metadata`` stays unchanged. This wrapper reads the same
    yt-dlp JSON and keeps the music fields the Music hub needs.
    """
    try:
        result = subprocess.run(
            build_ytdlp_command(url, cookies_from_browser),
            capture_output=True,
            text=True,
            encoding="utf-8",
            creationflags=_CREATION_FLAGS,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        logger.warning("yt-dlp timed out after %ss: %s", timeout_seconds, url)
        return None
    except FileNotFoundError:
        raise
    except OSError as exc:
        logger.warning("yt-dlp could not start for %s: %s", url, exc)
        return None

    if result.returncode != 0 or not result.stdout.strip():
        logger.info("Dead or blocked link (yt-dlp rc=%s): %s", result.returncode, url)
        return None
    try:
        info = json.loads(result.stdout.strip().splitlines()[0])
    except json.JSONDecodeError as exc:
        logger.warning("yt-dlp returned invalid JSON for %s: %s", url, exc)
        return None
    tags = info.get("tags") or []
    if not isinstance(tags, list):
        tags = []
    return {
        "url": url,
        "title": info.get("title") or "",
        "description": info.get("description") or "",
        "tags": [str(tag) for tag in tags if tag],
        "creator": info.get("uploader") or info.get("artist") or "Unknown",
        "artist": info.get("artist") or "",
        "track": info.get("track") or "",
        "album": info.get("album") or "",
    }


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
        prompt="Transcribe spoken instructions in this clip. If it is silent or pure music, say so in a few words.",
        audio=(audio_bytes, "audio/mp3"),
    )
    result = runtime.complete(request)
    return (result.text or "").strip() or None


def _downscale(image):
    """Cap the longest side so a keyframe stays a cheap vision input."""
    image.thumbnail((KEYFRAME_MAX_PX, KEYFRAME_MAX_PX))
    return image


def extract_keyframes(url: str, tmp_dir: str) -> list:
    """Tier 3: frames at 25%, 50%, and 75% of the smallest stream."""
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
    if not os.path.exists(video_path):
        return images
    cap = cv2.VideoCapture(video_path)
    try:
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        if total <= 0:
            return images
        seen: set[int] = set()
        for fraction in KEYFRAME_FRACTIONS:
            frame_num = min(total - 1, max(0, int(total * fraction)))
            if frame_num in seen:
                continue
            seen.add(frame_num)
            cap.set(cv2.CAP_PROP_POS_FRAMES, frame_num)
            ok, frame = cap.read()
            if not ok:
                continue
            image = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
            images.append(_downscale(image))
    finally:
        cap.release()
    return images


def _fill_music_fields(meta: dict, analysis: ModelCAnalysis) -> ModelCAnalysis:
    """Prefer yt-dlp artist/track when the model left them blank."""
    if not (analysis.artist or "").strip() and meta.get("artist"):
        analysis.artist = str(meta["artist"])
    if not (analysis.sound or "").strip() and meta.get("track"):
        analysis.sound = str(meta["track"])
    return analysis


def analyze_video(runtime: LlmRuntime, settings: Settings, url: str) -> tuple[dict | None, ModelCAnalysis | None]:
    """Return ``(None, None)`` for a dead link. Rate limits and bad JSON propagate."""
    meta = fetch_model_c_metadata(url, settings.extract.timeout_seconds, settings.extract.cookies_from_browser)
    if meta is None:
        return None, None
    tags = meta["tags"] if isinstance(meta["tags"], list) else []
    combined = f"{meta['title']} {meta['description']} {' '.join(tags)}".strip()
    transcript = None
    frames: list = []
    with tempfile.TemporaryDirectory() as tmp_dir:
        if len(combined) < settings.model_c.sparse_text_chars:
            logger.info("  - Sparse caption. Trying audio (tier 2)...")
            transcript = extract_audio_transcript(runtime, settings, url, tmp_dir)
            if transcript_needs_vision(transcript):
                logger.info("  - Audio silent or music-only. Trying keyframes (tier 3)...")
                frames = extract_keyframes(url, tmp_dir)

        prompt = (
            "Analyze this saved video for an Obsidian Second Brain under Model C.\n"
            f"Target categories: {MODEL_C_CATEGORIES}.\n"
            "actionable_items must be usable as-is:\n"
            "- Music: artist and track or sound lines\n"
            "- Fitness & Health: one exercise with reps or cues per item\n"
            "- Recipes & Food: ingredients and preparation steps\n"
            "If this does not fit those three, or context is missing, set "
            "needs_manual_review=true and category to Other / Ambiguous.\n"
            f"Title: {meta['title']}\n"
            f"Caption: {meta['description']}\n"
            f"Tags: {', '.join(tags)}\n"
            f"Artist: {meta.get('artist') or 'Unknown'}\n"
            f"Track: {meta.get('track') or 'Unknown'}\n"
            f"Audio Transcript: {transcript or 'N/A'}\n"
        )
        request = CompletionRequest(
            prompt=prompt,
            response_schema=ModelCAnalysis,
            extra_image_objects=frames,
        )
        result = runtime.complete(request)
    analysis = _fill_music_fields(meta, ModelCAnalysis.model_validate_json(result.text or ""))
    return meta, analysis


def _checkbox_lines(items: list[str]) -> str:
    cleaned = [" ".join(item.split()) for item in items if item and item.strip()]
    if not cleaned:
        return "- [ ]"
    return "\n".join(f"- [ ] {item}" for item in cleaned)


def _body_section(category_label: str, analysis: ModelCAnalysis, meta: dict) -> str:
    if category_label == "Fitness & Health":
        return f"## Exercises & Movement Routine\n{_checkbox_lines(analysis.actionable_items)}\n"
    if category_label == "Recipes & Food":
        return f"## Ingredients & Preparation\n{_checkbox_lines(analysis.actionable_items)}\n"
    if category_label == "Music":
        artist = analysis.artist or meta.get("artist") or "Unknown"
        sound = analysis.sound or meta.get("track") or "Unknown"
        return f"## Artist & Sound\n- **Artist:** {artist}\n- **Sound:** {sound}\n"
    return f"## Review\n{analysis.summary or 'Needs manual review.'}\n"


def render_note(meta: dict, analysis: ModelCAnalysis, url: str, status: str, category_label: str) -> str:
    """Markdown card. Model text is never used as a filesystem path."""
    artist = analysis.artist or meta.get("artist") or ""
    sound = analysis.sound or meta.get("track") or ""
    music_keys = ""
    if category_label == "Music":
        music_keys = f"artist: {yaml_str(artist)}\nsound: {yaml_str(sound)}\n"
    title = meta.get("title") or analysis.summary or "Untitled"
    creator = meta.get("creator") or "Unknown"
    return f"""---
category: {yaml_str(category_label)}
creator: {yaml_str(creator)}
url: {yaml_str(url)}
status: {yaml_str(status)}
confidence: {analysis.confidence}
summary: {yaml_str(analysis.summary)}
{music_keys}tags:
  - second-brain
  - {category_tag(category_label)}
---

# {title}

> **Summary:** {analysis.summary}

- **Creator:** @{creator}
- **Source Link:** [{url}]({url})

{_body_section(category_label, analysis, meta)}
"""


def render_unavailable(url: str) -> str:
    """Manual Review card for a link yt-dlp could not read. No model fields."""
    return f"""---
category: "Ambiguous"
creator: "Unknown"
url: {yaml_str(url)}
status: "manual-review"
confidence: 0.0
summary: "Dead or unavailable link."
tags:
  - second-brain
  - category/ambiguous
---

# Dead link

> **Summary:** Dead or unavailable link.

- **Source Link:** [{url}]({url})

## Review
The link could not be fetched (removed, private, or timed out). No model call was made.
"""


def _pop_inbox_line(inbox, line: str) -> None:
    """Remove one processed line, re-reading so concurrent appends survive."""
    lines = [ln.strip() for ln in inbox.read_text(encoding="utf-8").splitlines() if ln.strip()]
    lines = [ln for ln in lines if ln != line]
    atomic_write_text(inbox, "".join(f"{ln}\n" for ln in lines))


def run_ingest(settings: Settings, runtime: LlmRuntime | None = None) -> IngestResult:
    result = IngestResult()
    settings.require_vault()
    ensure_hubs(settings.resources_dir)
    inbox = settings.model_c_inbox_path
    if not inbox.exists():
        logger.info("No Model C pending file at %s", inbox)
        return result
    lines = [ln.strip() for ln in inbox.read_text(encoding="utf-8").splitlines() if ln.strip()]
    if not lines:
        logger.info("Model C inbox is empty.")
        return result

    ledger = UrlLedger(settings.ledger_path)
    known = get_vault_urls(settings.resources_dir) | ledger.urls
    runtime = runtime or build_runtime(settings)
    logger.info("Processing %d link(s) for Model C...", len(lines))

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
            result.stopped_reason = f"Unparseable model response: {exc}"
        if result.stopped_reason:
            logger.error("Stopping Model C ingest: %s. Remaining links stay in the inbox.", result.stopped_reason)
            break

        if meta is None or analysis is None:
            path = write_card(
                settings.resources_dir / "Inbox" / "Manual Review",
                "Dead link",
                render_unavailable(url),
            )
            ledger.record(url, EVENT_CARDED, category="Ambiguous", source="model-c-ingest")
            known.add(url)
            _pop_inbox_line(inbox, line)
            result.created += 1
            logger.info("  -> Dead link filed for review: %s", path.name)
            continue

        chosen = route(
            settings.resources_dir,
            analysis.category,
            analysis.confidence,
            analysis.needs_manual_review,
            settings.model_c.confidence_threshold,
        )
        stem = sanitize_filename(meta.get("title") or analysis.summary, max_len=50, fallback="Untitled Video")
        path = write_card(
            chosen.target_dir,
            stem,
            render_note(meta, analysis, url, chosen.status, chosen.category_label),
        )
        ledger.record(url, EVENT_CARDED, category=chosen.category_label, source="model-c-ingest")
        known.add(url)
        _pop_inbox_line(inbox, line)
        result.created += 1
        logger.info("  -> Created %s [status: %s]", path.name, chosen.status)

    return result
