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
from ..urls import normalize_url
from .probe import ProbeResult, count_scene_changes, probe_file

logger = logging.getLogger(__name__)

_CREATION_FLAGS = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
_COOKIE_DB_LOCKED = "cookie database"
_cookie_fallback_warned = False


def _run_ytdlp(cmd: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
    """Run yt-dlp. If the browser cookie DB is locked, retry once without cookies."""

    def once(argv: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=_CREATION_FLAGS,
        )

    result = once(cmd)
    if settings_cookies_failed(result.stderr or "") and "--cookies-from-browser" in cmd:
        global _cookie_fallback_warned
        if not _cookie_fallback_warned:
            _cookie_fallback_warned = True
            logger.warning(
                "Browser cookies could not be read (cookie database locked). Retrying without cookies. "
                "Close Edge/Chrome completely and re-run if Instagram carousels still have no images."
            )
        cleaned: list[str] = []
        skip_next = False
        for part in cmd:
            if skip_next:
                skip_next = False
                continue
            if part == "--cookies-from-browser":
                skip_next = True
                continue
            cleaned.append(part)
        result = once(cleaned)
    return result


def settings_cookies_failed(stderr: str) -> bool:
    text = (stderr or "").lower()
    return _COOKIE_DB_LOCKED in text or "could not copy chrome cookie" in text


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
        result = _run_ytdlp(cmd, timeout=60)
    except (subprocess.SubprocessError, OSError) as exc:
        logger.warning("yt-dlp info failed for %s: %s", url, exc)
        return None
    if not result.stdout.strip():
        return None
    # Instagram carousels log per-slide errors then emit playlist JSON on the last line.
    for line in reversed(result.stdout.strip().splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            continue
    return None


def _resolve_webpage_url(url: str, settings: CrusherSettings) -> str:
    """Follow redirects (TikTok t/ short links -> canonical /video/ or /photo/)."""
    # yt-dlp often fails to expand vm/t short links. A plain GET follows them.
    if "tiktok.com/t/" in url or "vm.tiktok.com" in url or "vt.tiktok.com" in url:
        try:
            req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urlopen(req, timeout=20) as resp:
                final = resp.geturl()
            if final and final != url:
                return final.split("?")[0]
        except (URLError, OSError, ValueError) as exc:
            logger.debug("Short-link resolve failed for %s: %s", url, exc)
    cmd = ["yt-dlp", "--skip-download", "--print", "webpage_url"]
    if settings.cookies_from_browser:
        cmd += ["--cookies-from-browser", settings.cookies_from_browser]
    cmd += ["--", url]
    try:
        result = _run_ytdlp(cmd, timeout=45)
    except (subprocess.SubprocessError, OSError):
        return url
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip().splitlines()[-1].strip()
    return url


def _minimal_info_from_url(url: str) -> dict:
    return {"title": "", "description": "", "tags": [], "uploader": "Unknown", "url": url}


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
                    _run_ytdlp(cmd, timeout=45)
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
        result = _run_ytdlp(cmd, timeout=120)
    except (subprocess.SubprocessError, OSError) as exc:
        logger.info("Video download skipped: %s", exc)
        return None
    found = _first_media_file(tmp)
    if found:
        return found
    # Format filter can miss TikTok HEVC-only ladders; try yt-dlp's default merge.
    fallback = ["yt-dlp", "-o", str(out), "--max-filesize", f"{int(settings.max_download_mb)}M", "--", url]
    if settings.cookies_from_browser:
        fallback[1:1] = ["--cookies-from-browser", settings.cookies_from_browser]
    try:
        _run_ytdlp(fallback, timeout=120)
    except (subprocess.SubprocessError, OSError) as exc:
        logger.info("Video fallback download skipped: %s", exc)
        if result.stderr:
            logger.debug("yt-dlp: %s", (result.stderr or "")[-400:])
        return None
    return _first_media_file(tmp)


def _first_media_file(tmp: Path) -> Path | None:
    for path in tmp.iterdir():
        if path.suffix.lower() in {".mp4", ".webm", ".mkv", ".m4a"} and path.is_file() and path.stat().st_size > 0:
            return path
    return None


def _download_instagram_slides(url: str, count: int, tmp: Path, settings: CrusherSettings) -> list[Path]:
    """Download each img_index slide when playlist entries lack direct URLs."""
    paths: list[Path] = []
    base = url.split("?")[0]
    for index in range(1, min(count, 15) + 1):
        slide_url = f"{base}?img_index={index}"
        out = tmp / f"ig_slide_{index:02d}.%(ext)s"
        cmd = ["yt-dlp", "--skip-download", "--write-thumbnail", "-o", str(out), "--", slide_url]
        if settings.cookies_from_browser:
            cmd[1:1] = ["--cookies-from-browser", settings.cookies_from_browser]
        try:
            _run_ytdlp(cmd, timeout=60)
        except (subprocess.SubprocessError, OSError):
            continue
        for candidate in tmp.glob(f"ig_slide_{index:02d}*"):
            if candidate.is_file():
                paths.append(candidate)
                break
    return paths


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
    playlist_count = info.get("playlist_count")
    if "instagram.com" in url and playlist_count:
        ig_paths = _download_instagram_slides(url, int(playlist_count), tmp, settings)
        if ig_paths:
            return ig_paths
    # Fallback: ask yt-dlp to dump thumbnails for the playlist URL.
    cmd = ["yt-dlp", "--skip-download", "--write-thumbnail", "-o", str(tmp / "thumb"), "--", url]
    if settings.cookies_from_browser:
        cmd[1:1] = ["--cookies-from-browser", settings.cookies_from_browser]
    try:
        _run_ytdlp(cmd, timeout=60)
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
    resolved = _resolve_webpage_url(url, settings)
    info = _run_ytdlp_json(url, settings)
    if info is None and resolved != url:
        info = _run_ytdlp_json(resolved, settings)
    if info is None:
        # TikTok /photo/ posts often fail yt-dlp JSON; still try slide scrape.
        if "/photo/" in resolved or "tiktok.com/t/" in url:
            info = _minimal_info_from_url(resolved)
        else:
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

    photo_target = resolved if "/photo/" in resolved else url
    if not result.carousel_image_paths and ("/photo/" in photo_target or "/photo/" in resolved):
        result.is_carousel = True
        result.carousel_image_paths = _tiktok_photo_fallback(photo_target, tmp_path)
        if not result.carousel_image_paths and resolved != photo_target:
            result.carousel_image_paths = _tiktok_photo_fallback(resolved, tmp_path)

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
        # Degraded path: caption/description-only (e.g. Instagram carousel when
        # slide downloads fail without browser cookies). Multimodal quality is
        # lower; completeness may land in needs-review.
        if (result.description or result.title).strip():
            logger.warning(
                "No local media for %s; continuing with metadata/caption only. "
                "Set crusher.cookies_from_browser for Instagram carousels.",
                url,
            )
            return result
        raise MediaUnavailableError("No downloadable video or images for this URL.")

    return result
