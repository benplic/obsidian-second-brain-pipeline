"""Gemini multimodal analysis for one acquired video."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path

from google.genai import errors as genai_errors
from pydantic import ValidationError

from ..config import CrusherSettings
from ..gemini import DailyQuotaExhaustedError, RateLimitExhaustedError, call_with_backoff
from .acquire import AcquiredMedia
from .budget import BudgetManager
from .schema import CrusherAnalysis

logger = logging.getLogger(__name__)


@dataclass
class AnalyzeContext:
    current_category: str | None
    caption: str
    creator: str
    subtitle_text: str
    pass_index: int = 0
    missing_hint: str | None = None


def _effective_fps(settings: CrusherSettings, duration: float, pass_index: int) -> float:
    base = settings.short_video_fps if duration <= settings.short_video_max_seconds else settings.video_fps
    return base * (1.0 + pass_index * 0.5)


def _read_bytes(path: Path, max_mb: float) -> bytes | None:
    if not path.is_file():
        return None
    size_mb = path.stat().st_size / (1024 * 1024)
    if size_mb > max_mb:
        return None
    return path.read_bytes()


def _upload_file(client, path: Path, settings: CrusherSettings, mime: str):
    from google.genai import types

    uploaded = client.files.upload(file=path, config=types.UploadFileConfig(mime_type=mime))
    deadline = time.time() + settings.file_poll_max_wait_seconds
    name = uploaded.name
    while time.time() < deadline:
        current = client.files.get(name=name)
        state = getattr(current, "state", None)
        state_name = getattr(state, "name", str(state))
        if state_name == "ACTIVE":
            return current
        if state_name == "FAILED":
            raise RuntimeError(f"Gemini file processing failed for {path.name}")
        time.sleep(settings.file_poll_seconds)
    raise TimeoutError(f"Timed out waiting for Gemini file {path.name}")


def _build_prompt(ctx: AnalyzeContext, settings: CrusherSettings, taxonomy_keys: list[str]) -> str:
    extra = ""
    if ctx.missing_hint:
        extra = f"\nCOMPLETENESS RETRY: {ctx.missing_hint}\n"
    return (
        "You analyze saved short-form videos for an Obsidian second brain.\n"
        f"Allowed categories (exact names): {taxonomy_keys}\n"
        f"Current card category: {ctx.current_category or 'unknown'}\n"
        f"Creator: {ctx.creator}\n"
        f"Platform caption/description: {ctx.caption}\n"
        f"Subtitle/caption file text: {ctx.subtitle_text or 'N/A'}\n"
        f"{extra}\n"
        "Rules:\n"
        "- Watch/read the ENTIRE clip. Enumerate EVERY list item, slide, or destination.\n"
        "- Read all on-screen text (song lists, album lists, places, recipes).\n"
        "- Transcribe speech when present; if silent, rely on visuals + on-screen text + caption.\n"
        "- Content beats misleading hashtags/captions.\n"
        "- Do NOT guess background songs unless named on screen, in speech, or caption.\n"
        "- Keep original proper names; write summary and findings in English.\n"
        "- Set list_expected_count when the video claims a numbered list (e.g. top 10).\n"
        "- Set recategorize=false only when the current category is clearly correct.\n"
        f"- prompt_version={settings.prompt_version}\n"
    )


def _resolve_media_resolution(settings: CrusherSettings):
    from google.genai import types

    raw = settings.media_resolution
    if isinstance(raw, str) and hasattr(types.MediaResolution, raw):
        return getattr(types.MediaResolution, raw)
    return types.MediaResolution.MEDIA_RESOLUTION_LOW


def _media_parts(client, media: AcquiredMedia, settings: CrusherSettings, fps: float) -> list:
    from google.genai import types

    parts: list = []
    resolution = _resolve_media_resolution(settings)

    if media.carousel_image_paths:
        for idx, img_path in enumerate(media.carousel_image_paths):
            data = _read_bytes(img_path, settings.inline_max_mb)
            if data:
                parts.append(types.Part.from_bytes(data=data, mime_type="image/jpeg", media_resolution=resolution))
                parts.append(f"Carousel slide index {idx}.")
        return parts

    if media.video_path and media.video_path.is_file():
        data = _read_bytes(media.video_path, settings.inline_max_mb)
        mime = "video/mp4"
        if data:
            parts.append(
                types.Part(
                    inline_data=types.Blob(data=data, mime_type=mime),
                    media_resolution=resolution,
                    video_metadata=types.VideoMetadata(fps=fps),
                )
            )
        else:
            uploaded = _upload_file(client, media.video_path, settings, mime)
            parts.append(
                types.Part(
                    file_data=types.FileData(file_uri=uploaded.uri, mime_type=mime),
                    media_resolution=resolution,
                    video_metadata=types.VideoMetadata(fps=fps),
                )
            )
    return parts


def analyze_media(
    client,
    budget: BudgetManager,
    key_var: str,
    settings: CrusherSettings,
    media: AcquiredMedia,
    ctx: AnalyzeContext,
    taxonomy_keys: list[str],
) -> CrusherAnalysis:
    from google.genai import types

    duration = media.probe.duration_seconds or 30.0
    fps = _effective_fps(settings, duration, ctx.pass_index)
    image_count = len(media.carousel_image_paths)
    tokens = budget.estimate_tokens(duration_seconds=duration, fps=fps, image_count=image_count)
    budget.wait_for_slot(estimated_tokens=tokens)

    prompt = _build_prompt(ctx, settings, taxonomy_keys)
    parts = [prompt, *_media_parts(client, media, settings, fps)]

    def _generate(model: str):
        # 503 is "try again", not a bad request. call_with_backoff only retries 429.
        last_exc: genai_errors.APIError | None = None
        for attempt in range(3):
            try:
                return client.models.generate_content(
                    model=model,
                    contents=parts,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_schema=CrusherAnalysis,
                        media_resolution=_resolve_media_resolution(settings),
                    ),
                )
            except genai_errors.APIError as exc:
                if exc.code not in {500, 503} or attempt == 2:
                    raise
                last_exc = exc
                delay = settings.backoff_base_seconds * (attempt + 1)
                logger.warning("Gemini %s (attempt %d/3). Waiting %.0fs...", exc.code, attempt + 1, delay)
                time.sleep(delay)
        raise last_exc  # pragma: no cover

    def _call():
        return _generate(settings.model)

    try:
        response = call_with_backoff(
            _call,
            max_retries=5,
            base_seconds=settings.backoff_base_seconds,
        )
    except DailyQuotaExhaustedError:
        budget.mark_daily_exhausted(key_var)
        raise
    except genai_errors.APIError as exc:
        if exc.code == 429:
            raise RateLimitExhaustedError(str(exc)) from exc
        raise

    budget.record_request(key_var, tokens=tokens)
    try:
        return CrusherAnalysis.model_validate_json(response.text or "")
    except ValidationError as exc:
        if settings.fallback_model:
            logger.warning("Primary model JSON invalid; trying fallback %s", settings.fallback_model)
            response = call_with_backoff(
                lambda: client.models.generate_content(
                    model=settings.fallback_model,
                    contents=parts,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_schema=CrusherAnalysis,
                    ),
                ),
                max_retries=3,
                base_seconds=settings.backoff_base_seconds,
            )
            budget.record_request(key_var, tokens=tokens)
            return CrusherAnalysis.model_validate_json(response.text or "")
        raise exc
