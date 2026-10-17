"""Tiered, budget-first media acquisition for the crusher.

Each URL escalates only as far as it has to:

- T0 metadata: one ``yt-dlp --dump-single-json`` call (title, caption, duration,
  caption-track URLs, carousel entries).
- T1 captions: fetch the caption track URL straight from the T0 JSON with a
  plain GET. No extra yt-dlp process, no media download, $0.
- T2 audio: only when captions are missing/too short. Downloads the smallest
  audio stream and transcribes it locally (faster-whisper).
- T3 visuals: only when ``gate.decide_visuals`` says the content is on screen
  (slideshows, silent clips, on-screen lists, carousels). Downloads a <=360p
  video and keeps a capped set of <=512px keyframes.

Full-resolution video is never downloaded.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Protocol
from urllib.error import URLError
from urllib.request import Request, urlopen

from ..config import CrusherSettings
from ..urls import normalize_url
from .probe import ProbeResult, count_scene_changes, probe_file
from .ytdlp import PermanentMediaError, TransientFetchError, YtDlpRunner, validate_url

logger = logging.getLogger(__name__)

_CREATION_FLAGS = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"

# Back-compat name used by run.py / tests. Permanent = never retry.
MediaUnavailableError = PermanentMediaError

TIER_METADATA = 0
TIER_CAPTIONS = 1
TIER_AUDIO = 2
TIER_VISUAL = 3

# Keys we keep from yt-dlp info JSON. The full JSON (formats list) can be MBs.
_INFO_KEYS = (
    "title",
    "description",
    "uploader",
    "channel",
    "tags",
    "duration",
    "_type",
    "entries",
    "playlist_count",
    "subtitles",
    "automatic_captions",
    "webpage_url",
)
_AUDIO_EXTS = {".m4a", ".mp3", ".aac", ".opus", ".ogg", ".webm", ".mp4", ".mkv", ".mov"}
_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}


class StageCache(Protocol):
    """Implemented by ``state.CrusherState``; kept as a protocol to avoid an import cycle."""

    def load_stage(self, url: str, stage: str) -> dict | None: ...

    def save_stage(self, url: str, stage: str, payload: dict) -> None: ...


class Transcriber(Protocol):
    def available(self) -> bool: ...

    def transcribe(self, path: Path): ...


@dataclass
class AcquiredMedia:
    url: str
    title: str = ""
    description: str = ""
    creator: str = "Unknown"
    tags: list[str] = field(default_factory=list)
    resolved_url: str = ""
    duration_seconds: float = 0.0
    is_carousel: bool = False
    carousel_image_paths: list[Path] = field(default_factory=list)
    # Only set when visual_mode == "video" (raw clip sent to Gemini).
    video_path: Path | None = None
    # Low-bitrate file from T2; may be muxed (TikTok has no audio-only stream).
    av_path: Path | None = None
    keyframe_paths: list[Path] = field(default_factory=list)
    # Low-res file keyframes were cut from; reused for denser retries (no re-download).
    visual_source: Path | None = None
    transcript_text: str = ""
    transcript_source: str = "none"  # captions | whisper | none
    speech_ratio: float | None = None  # None = unknown (captions path never measures it)
    tier_reached: int = TIER_METADATA
    probe: ProbeResult = field(default_factory=ProbeResult)
    info: dict = field(default_factory=dict)
    unavailable_reason: str | None = None
    tmp_dir: tempfile.TemporaryDirectory | None = None

    # Older callers read ``subtitle_text``.
    @property
    def subtitle_text(self) -> str:
        return self.transcript_text

    @property
    def transcript_words(self) -> int:
        return len(self.transcript_text.split())

    @property
    def is_photo_post(self) -> bool:
        return "/photo/" in (self.resolved_url or self.url)

    @property
    def tmp_path(self) -> Path:
        if self.tmp_dir is None:
            self.tmp_dir = tempfile.TemporaryDirectory(prefix="crusher_")
        return Path(self.tmp_dir.name)

    def cleanup(self) -> None:
        """Delete downloaded media. Safe to call twice."""
        if self.tmp_dir is not None:
            try:
                self.tmp_dir.cleanup()
            except OSError as exc:
                # Windows can hold a handle briefly (AV scanners, ffmpeg). Not fatal.
                logger.debug("Temp cleanup failed (%s); OS will reclaim it.", exc)
            self.tmp_dir = None


# ---------------------------------------------------------------------------
# T0: metadata
# ---------------------------------------------------------------------------

_resolved_cache: dict[str, str] = {}


def resolve_short_url(url: str) -> str:
    """Expand TikTok short links (t/, vm., vt.) with a plain GET redirect follow.

    yt-dlp often fails to expand them; one GET is cheaper than a yt-dlp process.
    """
    if url in _resolved_cache:
        return _resolved_cache[url]
    resolved = url
    if "tiktok.com/t/" in url or "vm.tiktok.com" in url or "vt.tiktok.com" in url:
        try:
            with urlopen(Request(url, headers={"User-Agent": _USER_AGENT}), timeout=20) as resp:
                final = resp.geturl()
            if final and final != url:
                resolved = final.split("?")[0]
        except (URLError, OSError, ValueError) as exc:
            logger.debug("Short-link resolve failed for %s: %s", url, exc)
    _resolved_cache[url] = resolved
    return resolved


def _trim_info(info: dict) -> dict:
    trimmed = {k: info.get(k) for k in _INFO_KEYS if k in info}
    entries = trimmed.get("entries")
    if isinstance(entries, list):
        trimmed["entries"] = [
            {"url": e.get("url"), "thumbnail": e.get("thumbnail")} for e in entries if isinstance(e, dict)
        ]
    return trimmed


def fetch_info(url: str, runner: YtDlpRunner, cache: StageCache | None) -> tuple[dict, str]:
    """Return (trimmed info JSON, resolved URL). Raises Permanent/TransientFetchError."""
    if cache is not None:
        cached = cache.load_stage(url, "info")
        if cached and isinstance(cached.get("info"), dict):
            return cached["info"], str(cached.get("resolved_url") or url)

    resolved = resolve_short_url(url)
    info = runner.info_json(url)
    if info is None and resolved != url:
        info = runner.info_json(resolved)
    if info is None:
        if "/photo/" in resolved:
            # TikTok /photo/ posts often have no yt-dlp extractor; slides are scraped in T3.
            info = {"title": "", "description": "", "tags": [], "uploader": "Unknown"}
        else:
            # yt-dlp exited cleanly but printed no JSON. Could be a hiccup or a
            # dead link; run.py marks it unavailable after repeated attempts.
            raise TransientFetchError("yt-dlp returned no metadata JSON.")
    trimmed = _trim_info(info)
    if cache is not None:
        cache.save_stage(url, "info", {"info": trimmed, "resolved_url": resolved})
    return trimmed, resolved


# ---------------------------------------------------------------------------
# T1: captions from the info JSON (direct GET, $0)
# ---------------------------------------------------------------------------


def _fmt_ts(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"


def _vtt_time(value: str) -> float:
    parts = value.strip().split(":")
    try:
        nums = [float(p.replace(",", ".")) for p in parts]
    except ValueError:
        return 0.0
    total = 0.0
    for num in nums:
        total = total * 60 + num
    return total


def vtt_to_text(vtt: str, *, marker_every_seconds: float = 10.0) -> str:
    """Flatten WebVTT to text with a coarse ``[m:ss]`` marker every ~10s.

    Markers let the summarizer align speech with keyframes without paying for
    per-cue timestamps. Rolling auto-captions repeat lines, so consecutive
    duplicates are dropped.
    """
    out: list[str] = []
    last_line = ""
    last_marker = -marker_every_seconds
    cue_start = 0.0
    for raw in vtt.splitlines():
        line = raw.strip()
        if not line or line.startswith(("WEBVTT", "NOTE", "Kind:", "Language:", "STYLE")) or line.isdigit():
            continue
        if "-->" in line:
            cue_start = _vtt_time(line.split("-->")[0])
            continue
        line = re.sub(r"<[^>]+>", "", line).strip()
        if not line or line == last_line:
            continue
        if cue_start - last_marker >= marker_every_seconds:
            out.append(f"[{_fmt_ts(cue_start)}]")
            last_marker = cue_start
        out.append(line)
        last_line = line
    return " ".join(out)


def json3_to_text(raw: str, *, marker_every_seconds: float = 10.0) -> str:
    """YouTube json3 caption format -> text with coarse markers."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return ""
    out: list[str] = []
    last_marker = -marker_every_seconds
    for event in data.get("events") or []:
        text = "".join(seg.get("utf8", "") for seg in event.get("segs") or []).strip()
        if not text:
            continue
        start = float(event.get("tStartMs") or 0) / 1000.0
        if start - last_marker >= marker_every_seconds:
            out.append(f"[{_fmt_ts(start)}]")
            last_marker = start
        out.append(text)
    return " ".join(out)


def _caption_candidates(info: dict) -> list[tuple[str, str, dict]]:
    """Ordered (bucket, lang, entry): manual English, auto English, manual any, auto any.

    TikTok uses codes like ``eng-US``; YouTube uses ``en`` / ``en-orig``. Auto
    captions translated into English (``en-xx`` from a foreign original) rank
    below the original-language track only via the "any" buckets.
    """
    subs = info.get("subtitles") or {}
    auto = info.get("automatic_captions") or {}

    def english(lang: str) -> bool:
        low = lang.lower()
        return low == "en" or low.startswith(("en-", "eng"))

    ordered: list[tuple[str, str, dict]] = []
    for bucket_name, bucket, want_en in (
        ("manual", subs, True),
        ("auto", auto, True),
        ("manual", subs, False),
        ("auto", auto, False),
    ):
        if not isinstance(bucket, dict):
            continue
        langs = sorted(bucket.keys(), key=lambda lang: (lang.lower() != "en-orig", lang))
        for lang in langs:
            if english(lang) != want_en or lang == "live_chat":
                continue
            entries = bucket.get(lang) or []
            for ext in ("vtt", "json3"):
                for entry in entries:
                    if isinstance(entry, dict) and entry.get("ext") == ext and entry.get("url"):
                        ordered.append((bucket_name, lang, entry))
    return ordered


def _http_get_text(url: str, timeout: float = 20.0, max_bytes: int = 2_000_000) -> str:
    with urlopen(Request(url, headers={"User-Agent": _USER_AGENT}), timeout=timeout) as resp:
        return resp.read(max_bytes).decode("utf-8", errors="replace")


def fetch_captions(info: dict, *, max_tracks: int = 3) -> str:
    """Return caption text from the best track, or '' when none is usable.

    Tries at most ``max_tracks`` URLs so a video with 40 translated tracks does
    not turn into 40 requests.
    """
    for _bucket, lang, entry in _caption_candidates(info)[:max_tracks]:
        try:
            raw = _http_get_text(str(entry["url"]))
        except (URLError, OSError, ValueError) as exc:
            logger.debug("Caption GET failed (%s): %s", lang, exc)
            continue
        text = json3_to_text(raw) if entry.get("ext") == "json3" else vtt_to_text(raw)
        if text.strip():
            return text
    return ""


# ---------------------------------------------------------------------------
# T2: smallest audio stream + local transcription
# ---------------------------------------------------------------------------

# TikTok serves muxed mp4 only (no audio-only format), so "ba" can match
# nothing; "worst" then picks the smallest muxed file, which T3 can reuse.
AUDIO_FORMAT = "ba[ext=m4a]/ba/worst"


def _first_file(folder: Path, prefix: str, exts: set[str]) -> Path | None:
    for path in sorted(folder.glob(f"{prefix}*")):
        if path.is_file() and path.suffix.lower() in exts and path.stat().st_size > 0:
            return path
    return None


def acquire_audio(
    media: AcquiredMedia,
    settings: CrusherSettings,
    runner: YtDlpRunner,
    transcriber: Transcriber | None,
) -> None:
    """Download the low-bitrate stream and transcribe it. Mutates ``media``."""
    media.tier_reached = max(media.tier_reached, TIER_AUDIO)
    if media.is_carousel or media.is_photo_post:
        return  # image posts have no audio track worth paying for
    if transcriber is None or not transcriber.available():
        logger.warning(
            "faster-whisper is not installed; skipping audio transcription. "
            'Install with: pip install -e ".[transcribe]"'
        )
        return
    template = str(media.tmp_path / "av.%(ext)s")
    runner.download(media.url, fmt=AUDIO_FORMAT, output_template=template, max_filesize_mb=settings.audio_max_mb)
    path = _first_file(media.tmp_path, "av", _AUDIO_EXTS)
    if path is None:
        logger.info("No audio downloaded for %s (over audio_max_mb or no stream).", media.url)
        return
    media.av_path = path
    media.probe = probe_file(path)
    if media.probe.duration_seconds:
        media.duration_seconds = media.probe.duration_seconds
    if not media.probe.has_audio and media.probe.duration_seconds:
        media.speech_ratio = 0.0
        return
    try:
        result = transcriber.transcribe(path)
    except (RuntimeError, OSError, ValueError) as exc:
        # TODO: distinguish corrupt media from model load failures if this shows up in reports.
        logger.warning("Transcription failed for %s: %s", media.url, exc)
        return
    media.speech_ratio = result.speech_ratio
    if result.text.strip():
        media.transcript_text = result.text
        media.transcript_source = "whisper"


# ---------------------------------------------------------------------------
# T3: visuals (carousel images or capped low-res keyframes)
# ---------------------------------------------------------------------------


def _download_image(url: str, dest: Path) -> bool:
    try:
        with urlopen(Request(url, headers={"User-Agent": _USER_AGENT}), timeout=30) as resp:
            dest.write_bytes(resp.read())
        return True
    except (URLError, OSError, ValueError) as exc:
        logger.debug("Image download failed %s: %s", dest.name, exc)
        return False


def _download_instagram_slides(url: str, count: int, tmp: Path, runner: YtDlpRunner, cap: int) -> list[Path]:
    """Per-slide ``img_index`` thumbnails when playlist entries lack direct URLs."""
    paths: list[Path] = []
    base = url.split("?")[0]
    for index in range(1, min(count, cap) + 1):
        out = tmp / f"ig_slide_{index:02d}.%(ext)s"
        try:
            runner.run(["--skip-download", "--write-thumbnail", "-o", str(out)], f"{base}?img_index={index}")
        except (TransientFetchError, PermanentMediaError) as exc:
            # One missing slide should not sink the whole carousel.
            logger.debug("IG slide %d failed: %s", index, exc)
            continue
        found = _first_file(tmp, f"ig_slide_{index:02d}", _IMAGE_EXTS)
        if found:
            paths.append(found)
    return paths


def _tiktok_photo_fallback(url: str, tmp: Path, cap: int) -> list[Path]:
    """Best-effort TikTok /photo/ slide scrape.

    TODO: Re-check against real /photo/ URLs; TikTok page JSON changes often.
    """
    try:
        html = _http_get_text(url, timeout=30, max_bytes=5_000_000)
    except (URLError, OSError, ValueError) as exc:
        logger.debug("TikTok photo fallback failed: %s", exc)
        return []
    urls = re.findall(r'https://[^"\'\\s]+\.(?:jpeg|jpg|webp)(?:\?[^"\'\\s]*)?', html, flags=re.IGNORECASE)
    paths: list[Path] = []
    for idx, img_url in enumerate(dict.fromkeys(urls)):
        dest = tmp / f"photo_{idx:02d}.jpg"
        if _download_image(img_url, dest):
            paths.append(dest)
        if len(paths) >= cap:
            break
    return paths


def _carousel_images(media: AcquiredMedia, settings: CrusherSettings, runner: YtDlpRunner) -> list[Path]:
    tmp = media.tmp_path
    cap = max(settings.max_keyframes, 20)  # carousels are the content itself; keep all slides up to 20
    paths: list[Path] = []
    for idx, entry in enumerate(media.info.get("entries") or []):
        img_url = (entry or {}).get("url") or (entry or {}).get("thumbnail")
        if img_url and str(img_url).startswith("http"):
            dest = tmp / f"carousel_{idx:02d}.jpg"
            if _download_image(str(img_url), dest):
                paths.append(dest)
        if len(paths) >= cap:
            break
    if paths:
        return paths
    count = media.info.get("playlist_count")
    if "instagram.com" in media.url and count:
        paths = _download_instagram_slides(media.url, int(count), tmp, runner, cap)
        if paths:
            return paths
    if media.is_photo_post:
        paths = _tiktok_photo_fallback(media.resolved_url or media.url, tmp, cap)
    return paths


def _scale_filter(max_px: int) -> str:
    """Longest side <= max_px, then force even dimensions for the jpeg encoder.

    ``force_original_aspect_ratio`` avoids a nested ``if()`` whose commas ffmpeg
    can split as extra filters.
    """
    px = int(max_px)
    # min() stops a small source from being upscaled to the cap (that only adds tokens).
    return (
        f"scale=w='min({px},iw)':h='min({px},ih)':force_original_aspect_ratio=decrease,"
        "scale=trunc(iw/2)*2:trunc(ih/2)*2"
    )


def _ffmpeg(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
        creationflags=_CREATION_FLAGS,
    )


def _grab_frames(video: Path, pattern: Path, vf: str, *, limit: int, variable_rate: bool) -> subprocess.CompletedProcess[str] | None:
    """One ffmpeg stills pass. ``pattern`` uses forward slashes: on Windows a
    backslash before ``%03d`` makes image2 reject the path (EINVAL)."""
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(video),
        "-map",
        "0:v:0",
        "-an",
        "-vf",
        vf,
        "-frames:v",
        str(limit),
        "-q:v",
        "5",
        "-f",
        "image2",
        "-y",
        pattern.as_posix(),
    ]
    if variable_rate:
        # Insert before -frames so a scene-select pass keeps irregular timestamps.
        cmd[cmd.index("-frames:v"):cmd.index("-frames:v")] = ["-fps_mode", "vfr"]
    try:
        return _ffmpeg(cmd)
    except (subprocess.SubprocessError, OSError) as exc:
        logger.warning("Keyframe extraction failed for %s: %s", video.name, exc)
        return None


def extract_keyframes(video: Path, out_dir: Path, settings: CrusherSettings) -> list[Path]:
    """Scene-change frames plus a time floor, downscaled, evenly capped.

    A frame is kept on a scene cut (new slide / new list item) or when
    ``keyframe_interval_seconds`` passed since the last kept frame. If that
    yields more than ``max_keyframes`` we subsample evenly instead of truncating,
    so the last items of a "top 10" are not dropped.

    Scene select can emit nothing (audio-only input, or a clip shorter than the
    interval whose select graph image2 then rejects). A fixed ``fps`` grab is
    the fallback so a real video still yields frames.
    """
    if not probe_file(video).has_video:
        logger.info("No video stream in %s; keyframes skipped.", video.name)
        return []
    out_dir.mkdir(parents=True, exist_ok=True)
    interval = max(1.0, float(settings.keyframe_interval_seconds))
    cap = max(1, int(settings.max_keyframes))
    select = (
        f"select='isnan(prev_selected_t)+gt(scene\\,{settings.scene_threshold})"
        f"+gte(t-prev_selected_t\\,{interval})',{_scale_filter(settings.keyframe_max_px)}"
    )
    scene_pattern = out_dir / "kf_%03d.jpg"
    result = _grab_frames(video, scene_pattern, select, limit=cap * 4, variable_rate=True)
    frames = sorted(p for p in out_dir.glob("kf_*.jpg") if p.is_file() and p.stat().st_size > 0)
    if not frames:
        if result is not None and result.returncode != 0:
            logger.info(
                "Scene select produced no frames for %s (%s). Falling back to one frame every %.0fs.",
                video.name,
                (result.stderr or "").strip()[-180:],
                interval,
            )
        interval_pattern = out_dir / "iv_%03d.jpg"
        _grab_frames(
            video,
            interval_pattern,
            f"fps=1/{interval},{_scale_filter(settings.keyframe_max_px)}",
            limit=cap,
            variable_rate=False,
        )
        frames = sorted(p for p in out_dir.glob("iv_*.jpg") if p.is_file() and p.stat().st_size > 0)
    if not frames:
        logger.warning("No keyframes extracted from %s.", video.name)
        return []
    return evenly_sample(frames, cap)


def evenly_sample(items: list, cap: int) -> list:
    """Pick ``cap`` items spread across the list, always keeping first and last."""
    if cap <= 0:
        return []
    if len(items) <= cap:
        return list(items)
    if cap == 1:
        return [items[0]]
    step = (len(items) - 1) / (cap - 1)
    return [items[round(i * step)] for i in range(cap)]


def acquire_visuals(media: AcquiredMedia, settings: CrusherSettings, runner: YtDlpRunner) -> None:
    """T3. Mutates ``media`` with carousel images or keyframes."""
    media.tier_reached = TIER_VISUAL
    if media.is_carousel or media.is_photo_post:
        if not media.carousel_image_paths:
            media.carousel_image_paths = _carousel_images(media, settings, runner)
            media.is_carousel = bool(media.carousel_image_paths) or media.is_carousel
        return

    video = media.av_path if media.av_path and media.probe.has_video else None
    if video is None:
        height = int(settings.visual_video_max_height)
        # Video-only ladder. A bare ``worst`` matches audio-only m4a, which
        # ffmpeg then cannot turn into frames.
        fmt = f"worstvideo[height<={height}][ext=mp4]/worstvideo[height<={height}]/worstvideo"
        runner.download(
            media.url,
            fmt=fmt,
            output_template=str(media.tmp_path / "vis.%(ext)s"),
            max_filesize_mb=settings.max_download_mb,
        )
        video = _first_file(media.tmp_path, "vis", _AUDIO_EXTS)
        if video is None:
            logger.info("No low-res video available for %s; keyframes skipped.", media.url)
            return
        vis_probe = probe_file(video)
        if vis_probe.duration_seconds and not media.duration_seconds:
            media.duration_seconds = vis_probe.duration_seconds
        media.probe.has_video = vis_probe.has_video or media.probe.has_video
        media.probe.width, media.probe.height = vis_probe.width, vis_probe.height

    if settings.visual_mode == "video":
        media.video_path = video
    media.visual_source = video
    frame_dir = media.tmp_path / "frames"
    frame_dir.mkdir(exist_ok=True)
    media.keyframe_paths = extract_keyframes(video, frame_dir, settings)
    if not media.keyframe_paths and media.av_path is not None and video == media.av_path:
        # The audio-tier file can be a container ffmpeg will not turn into stills.
        # One low-res video download is the last attempt before giving up on frames.
        height = int(settings.visual_video_max_height)
        runner.download(
            media.url,
            fmt=f"worstvideo[height<={height}][ext=mp4]/worstvideo[height<={height}]/worstvideo",
            output_template=str(media.tmp_path / "vis.%(ext)s"),
            max_filesize_mb=settings.max_download_mb,
        )
        vis = _first_file(media.tmp_path, "vis", _AUDIO_EXTS)
        if vis is not None:
            media.visual_source = vis
            if settings.visual_mode == "video":
                media.video_path = vis
            media.keyframe_paths = extract_keyframes(vis, frame_dir, settings)
            video = vis
    slides = count_scene_changes(video, settings)
    if slides and slides > 1:
        media.probe.expected_slide_count = slides


def densify_keyframes(media: AcquiredMedia, settings: CrusherSettings) -> bool:
    """Completeness retry: halve the frame interval and double the cap (bounded).

    Returns False when there is nothing denser to extract (carousel, no source file).
    """
    if media.visual_source is None or not media.visual_source.is_file():
        return False
    denser = replace(
        settings,
        keyframe_interval_seconds=max(1.0, settings.keyframe_interval_seconds / 2),
        max_keyframes=min(settings.max_keyframes * 2, 48),
        # Lower threshold catches subtle slide changes (same background, new text).
        scene_threshold=max(0.1, settings.scene_threshold * 0.6),
    )
    frame_dir = media.tmp_path / f"frames_dense_{len(media.keyframe_paths)}"
    frame_dir.mkdir(exist_ok=True)
    frames = extract_keyframes(media.visual_source, frame_dir, denser)
    if len(frames) <= len(media.keyframe_paths):
        return False
    media.keyframe_paths = frames
    return True


# ---------------------------------------------------------------------------
# Entry point for T0 + T1 (+ T2 when captions are thin)
# ---------------------------------------------------------------------------


def acquire_text(
    url: str,
    settings: CrusherSettings,
    runner: YtDlpRunner,
    *,
    cache: StageCache | None = None,
    transcriber: Transcriber | None = None,
) -> AcquiredMedia:
    """Run the cheap tiers. Visuals are added later by ``acquire_visuals`` if the gate asks.

    Raises ``PermanentMediaError`` (dead link, too long) or ``TransientFetchError``.
    """
    url = validate_url(normalize_url(url))
    info, resolved = fetch_info(url, runner, cache)

    duration = info.get("duration")
    try:
        duration_f = float(duration) if duration is not None else 0.0
    except (TypeError, ValueError):
        duration_f = 0.0
    if duration_f > settings.max_video_seconds:
        raise PermanentMediaError(f"Video longer than crusher.max_video_seconds ({settings.max_video_seconds}s).")

    media = AcquiredMedia(
        url=url,
        title=str(info.get("title") or ""),
        description=str(info.get("description") or ""),
        creator=str(info.get("uploader") or info.get("channel") or "Unknown"),
        tags=[str(t) for t in (info.get("tags") or []) if t],
        resolved_url=resolved,
        duration_seconds=duration_f,
        is_carousel=info.get("_type") == "playlist" or bool(info.get("entries")) or "/photo/" in resolved,
        info=info,
    )

    cached = cache.load_stage(url, "transcript") if cache is not None else None
    if cached is not None:
        media.transcript_text = str(cached.get("text") or "")
        media.transcript_source = str(cached.get("source") or "none")
        media.speech_ratio = cached.get("speech_ratio")
        media.tier_reached = int(cached.get("tier") or TIER_CAPTIONS)
        if cached.get("duration"):
            media.duration_seconds = float(cached["duration"])
        return media

    media.tier_reached = TIER_CAPTIONS
    captions = fetch_captions(info)
    if captions:
        media.transcript_text = captions
        media.transcript_source = "captions"

    if media.transcript_words < settings.min_transcript_words and not media.is_carousel:
        acquire_audio(media, settings, runner, transcriber)
        if media.duration_seconds > settings.max_video_seconds:
            raise PermanentMediaError("Downloaded file exceeds max_video_seconds.")

    if cache is not None:
        cache.save_stage(
            url,
            "transcript",
            {
                "text": media.transcript_text,
                "source": media.transcript_source,
                "speech_ratio": media.speech_ratio,
                "tier": media.tier_reached,
                "duration": media.duration_seconds,
            },
        )
    return media
