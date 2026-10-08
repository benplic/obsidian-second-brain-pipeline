"""Gemini summarizer for one item: text (+ optional low-res frames) in, structured analysis out.

Budget design: the default input is transcript + caption text, which costs a
few thousand tokens. Carousel slides or capped <=512px keyframes are added only
when the visual gate escalated. The raw clip is sent only with
``crusher.visual_mode: video``, which is off by default and is the expensive
path the old pipeline used for every item.
"""

from __future__ import annotations

import logging
import mimetypes
import time
from dataclasses import dataclass
from pathlib import Path

from google.genai import errors as genai_errors
from pydantic import ValidationError

from ..config import CrusherSettings
from ..gemini import RateLimitExhaustedError, call_with_backoff
from ..taxonomy import format_category_hints
from .acquire import AcquiredMedia
from .budget import BudgetManager
from .schema import CrusherAnalysis

logger = logging.getLogger(__name__)

# Cleared the first time a model rejects thinking_level, so later calls skip it.
_thinking_supported = True


@dataclass
class AnalyzeContext:
    current_category: str | None
    caption: str
    creator: str
    subtitle_text: str
    pass_index: int = 0
    missing_hint: str | None = None


def _read_bytes(path: Path, max_mb: float) -> bytes | None:
    if not path.is_file():
        return None
    if path.stat().st_size / (1024 * 1024) > max_mb:
        return None
    return path.read_bytes()


def _upload_file(client, path: Path, settings: CrusherSettings, mime: str):
    from google.genai import types

    uploaded = client.files.upload(file=path, config=types.UploadFileConfig(mime_type=mime))
    deadline = time.time() + settings.file_poll_max_wait_seconds
    while time.time() < deadline:
        current = client.files.get(name=uploaded.name)
        state_name = getattr(getattr(current, "state", None), "name", "")
        if state_name == "ACTIVE":
            return current
        if state_name == "FAILED":
            raise RuntimeError(f"Gemini file processing failed for {path.name}")
        time.sleep(settings.file_poll_seconds)
    raise TimeoutError(f"Timed out waiting for Gemini file {path.name}")


def _visual_description(media: AcquiredMedia, settings: CrusherSettings) -> str:
    if media.carousel_image_paths:
        return f"{len(media.carousel_image_paths)} carousel/photo slides, in order."
    if media.video_path and settings.visual_mode == "video":
        return "The full low-resolution clip."
    if media.keyframe_paths:
        return (
            f"{len(media.keyframe_paths)} keyframes in time order, taken at scene changes and at least "
            f"every {settings.keyframe_interval_seconds:.0f}s. Each distinct slide or list item likely appears once."
        )
    return "None. Text only."


def build_prompt(
    ctx: AnalyzeContext,
    media: AcquiredMedia,
    settings: CrusherSettings,
    taxonomy_keys: list[str],
) -> str:
    extra = f"\nCOMPLETENESS RETRY: {ctx.missing_hint}\n" if ctx.missing_hint else ""
    text_only = not (media.carousel_image_paths or media.keyframe_paths or media.video_path)
    return (
        "You analyze saved short-form videos for an Obsidian second brain.\n"
        f"Allowed categories (exact names): {taxonomy_keys}\n"
        f"{format_category_hints(taxonomy_keys)}"
        f"Current card category: {ctx.current_category or 'unknown'}\n"
        f"Creator: {ctx.creator}\n"
        f"Platform caption/description: {ctx.caption}\n"
        f"Duration: {media.duration_seconds:.0f}s\n"
        f"Transcript source: {media.transcript_source}\n"
        f"Transcript ([m:ss] markers are approximate): {ctx.subtitle_text or 'N/A'}\n"
        f"Visual input: {_visual_description(media, settings)}\n"
        f"{extra}\n"
        "Rules:\n"
        "- Enumerate EVERY list item, slide, or destination you have evidence for.\n"
        "- Read all on-screen text in the images (song lists, album lists, places, recipes).\n"
        "- Content beats misleading hashtags/captions.\n"
        "- Do NOT guess background songs unless named on screen, in speech, or caption.\n"
        "- Never invent items to fill a claimed count; report what you saw and set list_expected_count.\n"
        "- Keep original proper names; write summary and findings in English.\n"
        "- Set list_expected_count when the video claims a numbered list (e.g. top 10).\n"
        "- For extracted tip items use kind health_tip (wellness/nutrition/sleep), "
        "study_tip (learning/study), or tip (crafts/DIY/life hacks).\n"
        "- Set recategorize=false only when the current category is clearly correct.\n"
        + (
            "- You only have text. Set needs_visuals=true if key content (list items, places, products, "
            "on-screen text) is likely shown on screen but missing from the text above.\n"
            if text_only
            else "- Set needs_visuals=false.\n"
        )
        + "- Leave relevance and content_tags empty; a separate classifier fills them.\n"
        f"- prompt_version={settings.prompt_version}\n"
    )


def _resolve_media_resolution(settings: CrusherSettings):
    from google.genai import types

    raw = settings.media_resolution
    if isinstance(raw, str) and hasattr(types.MediaResolution, raw):
        return getattr(types.MediaResolution, raw)
    return types.MediaResolution.MEDIA_RESOLUTION_LOW


def _image_mime(path: Path) -> str:
    return mimetypes.guess_type(path.name)[0] or "image/jpeg"


def media_parts(client, media: AcquiredMedia, settings: CrusherSettings) -> list:
    """Images first (carousel or keyframes); raw video only in ``visual_mode: video``."""
    from google.genai import types

    parts: list = []
    resolution = _resolve_media_resolution(settings)
    images = media.carousel_image_paths or media.keyframe_paths
    for idx, img_path in enumerate(images):
        data = _read_bytes(img_path, settings.inline_max_mb)
        if data:
            parts.append(types.Part.from_bytes(data=data, mime_type=_image_mime(img_path), media_resolution=resolution))
            parts.append(f"Image {idx + 1} of {len(images)}.")
    if images or settings.visual_mode != "video" or not media.video_path:
        return parts

    mime = "video/mp4"
    fps = settings.short_video_fps if media.duration_seconds <= settings.short_video_max_seconds else settings.video_fps
    data = _read_bytes(media.video_path, settings.inline_max_mb)
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


def estimate_tokens(prompt: str, media: AcquiredMedia, settings: CrusherSettings) -> int:
    """Pre-flight estimate for TPM pacing and the spend cap. Actuals come from usage_metadata."""
    text = len(prompt) // 4 + 400  # ~4 chars/token + schema overhead
    images = len(media.carousel_image_paths or media.keyframe_paths) * settings.estimated_tokens_per_image
    video = 0
    if settings.visual_mode == "video" and media.video_path and not images:
        # Low media resolution is ~100 tokens per sampled second (frames + audio).
        video = int(max(media.duration_seconds, 1.0) * settings.video_fps * 100)
    return text + images + video


def _config(settings: CrusherSettings):
    from google.genai import types

    kwargs = dict(
        response_mime_type="application/json",
        response_schema=CrusherAnalysis,
        media_resolution=_resolve_media_resolution(settings),
    )
    if _thinking_supported and settings.thinking_level:
        level = getattr(types.ThinkingLevel, str(settings.thinking_level).upper(), None)
        if level is not None:
            kwargs["thinking_config"] = types.ThinkingConfig(thinking_level=level)
    return types.GenerateContentConfig(**kwargs)


def _usage(response) -> tuple[int, int]:
    """(input, output) tokens. Thinking tokens bill as output, so they are counted there."""
    meta = getattr(response, "usage_metadata", None)
    if meta is None:
        return 0, 0
    prompt = int(getattr(meta, "prompt_token_count", 0) or 0)
    output = int(getattr(meta, "candidates_token_count", 0) or 0) + int(getattr(meta, "thoughts_token_count", 0) or 0)
    return prompt, output


def analyze_media(
    budget: BudgetManager,
    settings: CrusherSettings,
    media: AcquiredMedia,
    ctx: AnalyzeContext,
    taxonomy_keys: list[str],
) -> CrusherAnalysis:
    """One summary call, retried on the next Gemini key when the current key's daily quota is spent.

    Raises SpendCapReachedError before calling when over budget, and
    DailyQuotaExhaustedError only after every discovered key is exhausted.
    """
    prompt = build_prompt(ctx, media, settings, taxonomy_keys)
    tokens = estimate_tokens(prompt, media, settings)

    def _once(key_var: str) -> CrusherAnalysis:
        budget.check_spend(estimated_input_tokens=tokens)
        budget.wait_for_slot(estimated_tokens=tokens)
        client = budget.client_for(key_var)
        parts = [prompt, *media_parts(client, media, settings)]

        def _generate(model: str):
            # 503 is "try again", not a bad request. call_with_backoff only retries 429.
            global _thinking_supported
            last_exc: genai_errors.APIError | None = None
            for attempt in range(3):
                try:
                    return client.models.generate_content(model=model, contents=parts, config=_config(settings))
                except genai_errors.APIError as exc:
                    if exc.code == 400 and _thinking_supported and "thinking" in str(exc).lower():
                        logger.warning("Model %s rejected thinking_level; retrying without it.", model)
                        _thinking_supported = False
                        continue
                    if exc.code not in {500, 503} or attempt == 2:
                        raise
                    last_exc = exc
                    delay = settings.backoff_base_seconds * (attempt + 1)
                    logger.warning("Gemini %s (attempt %d/3). Waiting %.0fs...", exc.code, attempt + 1, delay)
                    time.sleep(delay)
            raise last_exc  # pragma: no cover

        try:
            response = call_with_backoff(
                lambda: _generate(settings.model),
                max_retries=5,
                base_seconds=settings.backoff_base_seconds,
            )
        except genai_errors.APIError as exc:
            if exc.code == 429:
                raise RateLimitExhaustedError(str(exc)) from exc
            raise

        in_tok, out_tok = _usage(response)
        budget.record_request(key_var, tokens=in_tok or tokens)
        budget.record_gemini_cost(in_tok or tokens, out_tok)
        try:
            return CrusherAnalysis.model_validate_json(response.text or "")
        except ValidationError:
            if not settings.fallback_model:
                raise
            logger.warning("Primary model JSON invalid; trying fallback %s", settings.fallback_model)
            response = call_with_backoff(
                lambda: _generate(settings.fallback_model),
                max_retries=3,
                base_seconds=settings.backoff_base_seconds,
            )
            in_tok, out_tok = _usage(response)
            budget.record_request(key_var, tokens=in_tok or tokens)
            budget.record_gemini_cost(in_tok or tokens, out_tok)
            return CrusherAnalysis.model_validate_json(response.text or "")

    return budget.call_rotating(_once)
