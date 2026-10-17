"""Model C helpers used by the shared dispatcher.

The queue is ``pending_links.txt``. ``run_ingest`` is a compatibility entry
point and does not read ``model_c_pending.txt``.
"""

from __future__ import annotations

import logging
import os
import subprocess

from ..config import Settings
from ..llm.runtime import LlmRuntime

logger = logging.getLogger(__name__)

_CREATION_FLAGS = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
MIN_TRANSCRIPT_CHARS = 20
KEYFRAME_FRACTIONS = (0.25, 0.50, 0.75)
KEYFRAME_MAX_PX = 512

_MUSIC_ONLY_MARKERS = (
    "instrumental",
    "no speech",
    "no spoken",
    "pure music",
    "music only",
    "just music",
)


def transcript_needs_vision(text: str | None) -> bool:
    """True when the text did not yield spoken instructions worth trusting alone."""
    cleaned = (text or "").strip()
    if len(cleaned) < MIN_TRANSCRIPT_CHARS:
        return True
    lowered = cleaned.lower()
    return any(marker in lowered for marker in _MUSIC_ONLY_MARKERS)


def _downscale(image):
    """Cap the longest side so a keyframe stays a cheap vision input."""
    image.thumbnail((KEYFRAME_MAX_PX, KEYFRAME_MAX_PX))
    return image


def extract_keyframes(url: str, tmp_dir: str) -> list:
    """Frames at 25%, 50%, and 75% of the smallest stream. Empty if OpenCV is missing."""
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


def run_ingest(settings: Settings, runtime: LlmRuntime | None = None):
    """Read the shared pending-links queue. The legacy Model C inbox is not popped."""
    from ..dispatch import run_dispatch

    return run_dispatch(settings, runtime)
