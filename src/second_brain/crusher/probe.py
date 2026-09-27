"""ffprobe/ffmpeg helpers for media shape detection."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ..config import CrusherSettings

logger = logging.getLogger(__name__)

_CREATION_FLAGS = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


class FfmpegMissingError(RuntimeError):
    """ffmpeg/ffprobe not on PATH."""


@dataclass
class ProbeResult:
    duration_seconds: float = 0.0
    has_audio: bool = False
    has_video: bool = False
    width: int = 0
    height: int = 0
    expected_slide_count: int | None = None


def ensure_ffmpeg() -> None:
    if shutil.which("ffprobe") and shutil.which("ffmpeg"):
        return
    hint = "Install ffmpeg (e.g. winget install ffmpeg) and ensure ffprobe is on PATH."
    raise FfmpegMissingError(hint)


def _run_json(cmd: list[str], *, timeout: int = 60) -> dict | None:
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout,
            creationflags=_CREATION_FLAGS,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        logger.debug("ffprobe/ffmpeg failed: %s", exc)
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


def probe_file(path: Path) -> ProbeResult:
    """Inspect one local media file with ffprobe."""
    info = _run_json(
        [
            "ffprobe",
            "-v",
            "quiet",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(path),
        ]
    )
    if not info:
        return ProbeResult()
    duration = 0.0
    fmt = info.get("format") or {}
    try:
        duration = float(fmt.get("duration") or 0.0)
    except (TypeError, ValueError):
        duration = 0.0
    has_audio = has_video = False
    width = height = 0
    for stream in info.get("streams") or []:
        codec_type = stream.get("codec_type")
        if codec_type == "audio":
            has_audio = True
        if codec_type == "video":
            has_video = True
            width = int(stream.get("width") or 0)
            height = int(stream.get("height") or 0)
    return ProbeResult(
        duration_seconds=duration,
        has_audio=has_audio,
        has_video=has_video,
        width=width,
        height=height,
    )


def count_scene_changes(video_path: Path, settings: CrusherSettings) -> int | None:
    """Count distinct slides in a rendered slideshow video via ffmpeg scene detection."""
    threshold = settings.scene_threshold
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-i",
        str(video_path),
        "-vf",
        f"select='gt(scene,{threshold})',showinfo",
        "-f",
        "null",
        "-",
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            creationflags=_CREATION_FLAGS,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        logger.debug("Scene detection failed: %s", exc)
        return None
    # Each scene change line in stderr; add 1 for the first slide.
    matches = re.findall(r"Parsed_showinfo", result.stderr or "")
    if not matches:
        return None
    return len(matches) + 1
