"""Download media, subtitles, and carousel images for multimodal analysis."""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

from ..config import CrusherSettings
from ..steps.extract_metadata import build_ytdlp_command
from ..urls import normalize_url
from .probe import ProbeResult, count_scene_changes, probe_file

logger = logging.getLogger(__name__)

_CREATION_FLAGS = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


class MediaUnavailableError(RuntimeError):
    """Video deleted, private, geo-blocked, or login-required."""


@dataclass
class AcquiredMedia:
    url: str
    title: str = ""
    description: str = ""
    creator: str = "Unknown"
    tags: list[str] = field(default_factory=list)
    is_carousel: bool = False
    carousel_image_paths: list[Path] = field(default_factory=list)
    video_path: Path | None = None
    subtitle_text: str = ""
    probe: ProbeResult = field(default_factory=ProbeResult)
    unavailable_reason: str | None = None
    tmp_dir: tempfile.TemporaryDirectory | None = None


def _run_ytdlp_json(url: str, settings: CrusherSettings) -> dict | None:
    cmd = ["yt-dlp", "--dump-single-json", "--skip-download", "--ignore-errors"]
    if settings.cookies_from_browser:
        cmd += ["--cookies-from-browser", settings.cookies_from_browser]
    cmd += ["--", url]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
            creationflags=_CREATION_FLAGS,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        logger.warning("yt-dlp info failed for %s: %s", url, exc)
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    try:
        return json.loads(result.stdout.strip().splitlines()[0])
    except json.JSONDecodeError:
        return None


def _vtt_to_text(vtt: str) -> str:
    lines = []
    for line in vtt.splitlines():
        line = line.strip()
        if not line or line.startswith("WEBVTT") or "-->" in line or line.isdigit():
            continue
        if re.match(r"^\d+$", line):
            continue
        lines.append(line)
    return " ".join(lines)


def _download_subtitles(info: dict, tmp: Path, url: str, settings: CrusherSettings) -> str:
    subs = info.get("subtitles") or {}
    auto = info.get("automatic_captions") or {}
    lang_pref = ["en", "en-US", "en-orig"]
    for bucket in (subs, auto):
        for lang in lang_pref + list(bucket.keys()):
            entries = bucket.get(lang)
            if not entries:
                continue
            for entry in entries:
                ext = entry.get("ext", "vtt")
                if ext not in {"vtt", "srv3", "json3"}:
                    continue
                out = tmp / f"subs.{ext}"
                cmd = [
                    "yt-dlp",
                    "--skip-download",
                    "--write-subs" if bucket is subs else "--write-auto-subs",
                    "--sub-langs",
                    lang,
                    "--sub-format",
                    "vtt",
                    "-o",
                    str(tmp / "subs"),
                    "--",
                    url,
                ]
                if settings.cookies_from_browser:
                    cmd[1:1] = ["--cookies-from-browser", settings.cookies_from_browser]
                try:
                    subprocess.run(cmd, capture_output=True, timeout=45, check=False, creationflags=_CREATION_FLAGS)
                except (subprocess.SubprocessError, OSError):
                    continue
                for candidate in tmp.glob("subs*"):
                    if candidate.is_file():
                        try:
                            return _vtt_to_text(candidate.read_text(encoding="utf-8", errors="replace"))
                        except OSError:
                            pass
    return ""


def _download_video(url: str, tmp: Path, settings: CrusherSettings) -> Path | None:
    out = tmp / "media.%(ext)s"
    cmd = [
        "yt-dlp",
        "-f",
        "worst[ext=mp4][height<=480]/worst[ext=mp4]/worst",
        "--max-filesize",
        f"{int(settings.max_download_mb)}M",
        "-o",
        str(out),
        "--",
        url,
    ]
    if settings.cookies_from_browser:
        cmd[1:1] = ["--cookies-from-browser", settings.cookies_from_browser]
    try:
        subprocess.run(cmd, capture_output=True, timeout=120, check=False, creationflags=_CREATION_FLAGS)
    except (subprocess.SubprocessError, OSError) as exc:
        logger.info("Video download skipped: %s", exc)
        return None
    for path in tmp.iterdir():
        if path.suffix.lower() in {".mp4", ".webm", ".mkv"} and path.is_file():
            return path
    return None


def _download_carousel_entries(info: dict, tmp: Path, url: str, settings: CrusherSettings) -> list[Path]:
    paths: list[Path] = []
    entries = info.get("entries") or []
    if not entries and info.get("_type") == "playlist":
        return paths
    for idx, entry in enumerate(entries):
        if not isinstance(entry, dict):
            continue
        img_url = entry.get("url") or entry.get("thumbnail")
        if not img_url or not str(img_url).startswith("http"):
            continue
        dest = tmp / f"carousel_{idx:02d}.jpg"
        try:
            req = Request(str(img_url), headers={"User-Agent": "obsidian-second-brain-pipeline/0.1"})
            with urlopen(req, timeout=30) as resp:
                dest.write_bytes(resp.read())
            paths.append(dest)
        except (URLError, OSError, ValueError) as exc:
            logger.debug("Carousel image %d failed: %s", idx, exc)
    if paths:
        return paths
    # Fallback: ask yt-dlp to dump thumbnails for the playlist URL.
    cmd = ["yt-dlp", "--skip-download", "--write-thumbnail", "-o", str(tmp / "thumb"), "--", url]
    if settings.cookies_from_browser:
        cmd[1:1] = ["--cookies-from-browser", settings.cookies_from_browser]
    try:
        subprocess.run(cmd, capture_output=True, timeout=60, check=False, creationflags=_CREATION_FLAGS)
    except (subprocess.SubprocessError, OSError):
        return paths
    return sorted(p for p in tmp.glob("thumb*") if p.is_file())


def _tiktok_photo_fallback(url: str, tmp: Path) -> list[Path]:
    """Best-effort TikTok /photo/ slide download.

    TODO: Re-check against real /photo/ URLs; TikTok page JSON changes often.
    """
    if "/photo/" not in url:
        return []
    try:
        req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urlopen(req, timeout=30) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except (URLError, OSError, ValueError) as exc:
        logger.debug("TikTok photo fallback failed: %s", exc)
        return []
    urls = re.findall(r'https://[^"\'\\s]+\.(?:jpeg|jpg|webp)(?:\?[^"\'\\s]*)?', html, flags=re.IGNORECASE)
    seen: set[str] = set()
    paths: list[Path] = []
    for idx, img_url in enumerate(urls):
        if img_url in seen:
            continue
        seen.add(img_url)
        dest = tmp / f"photo_{idx:02d}.jpg"
        try:
            with urlopen(Request(img_url, headers={"User-Agent": "Mozilla/5.0"}), timeout=30) as resp:
                dest.write_bytes(resp.read())
            paths.append(dest)
        except (URLError, OSError):
            continue
        if len(paths) >= 20:
            break
    return paths


def acquire_media(url: str, settings: CrusherSettings) -> AcquiredMedia:
    """Fetch metadata and local media files for one URL."""
    url = normalize_url(url)
    info = _run_ytdlp_json(url, settings)
    if info is None:
        raise MediaUnavailableError("Could not fetch video metadata (deleted, private, or blocked).")

    title = str(info.get("title") or "")
    description = str(info.get("description") or "")
    creator = str(info.get("uploader") or info.get("channel") or "Unknown")
    tags = [str(t) for t in (info.get("tags") or []) if t]

    duration = info.get("duration")
    if duration is not None:
        try:
            if float(duration) > settings.max_video_seconds:
                raise MediaUnavailableError(
                    f"Video longer than crusher.max_video_seconds ({settings.max_video_seconds}s)."
                )
        except (TypeError, ValueError):
            pass

    tmp = tempfile.TemporaryDirectory(prefix="crusher_")
    tmp_path = Path(tmp.name)
    result = AcquiredMedia(
        url=url,
        title=title,
        description=description,
        creator=creator,
        tags=tags,
        tmp_dir=tmp,
    )

    is_playlist = info.get("_type") == "playlist" or bool(info.get("entries"))
    if is_playlist:
        result.is_carousel = True
        result.carousel_image_paths = _download_carousel_entries(info, tmp_path, url, settings)

    if not result.carousel_image_paths and "/photo/" in url:
        result.is_carousel = True
        result.carousel_image_paths = _tiktok_photo_fallback(url, tmp_path)

    result.subtitle_text = _download_subtitles(info, tmp_path, url, settings)
    result.video_path = _download_video(url, tmp_path, settings)

    if result.video_path and result.video_path.is_file():
        result.probe = probe_file(result.video_path)
        if result.probe.duration_seconds > settings.max_video_seconds:
            raise MediaUnavailableError("Downloaded file exceeds max_video_seconds.")
        slides = count_scene_changes(result.video_path, settings)
        if slides:
            result.probe.expected_slide_count = slides

    if not result.video_path and not result.carousel_image_paths:
        raise MediaUnavailableError("No downloadable video or images for this URL.")

    return result
