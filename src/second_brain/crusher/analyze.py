"""Multimodal summarizer for one item: text (+ optional low-res frames) in, structured analysis out.

Budget design: the default input is transcript + caption text, which costs a
few thousand tokens. Carousel slides or capped <=512px keyframes are added only
when the visual gate escalated. The raw clip is sent only with
``crusher.visual_mode: video``, which is off by default and is the expensive
path the old pipeline used for every item.
"""

from __future__ import annotations

import logging
import mimetypes
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from ..config import CrusherSettings, Settings
from ..llm.runtime import LlmRuntime
from ..llm.types import CompletionRequest, ImageInput, VideoInput
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


def _image_mime(path: Path) -> str:
    return mimetypes.guess_type(path.name)[0] or "image/jpeg"


def build_completion_request(
    prompt: str,
    media: AcquiredMedia,
    settings: CrusherSettings,
    app_settings: Settings,
) -> CompletionRequest:
    """Map acquired media to adapter inputs (paths only; encoding is adapter-owned)."""
    images: list[ImageInput] = []
    paths = media.carousel_image_paths or media.keyframe_paths or []
    for idx, img_path in enumerate(paths):
        caption = f"Image {idx + 1} of {len(paths)}." if len(paths) > 1 else None
        images.append(ImageInput(path=img_path, mime_type=_image_mime(img_path), caption=caption))

    video: VideoInput | None = None
    if (
        settings.visual_mode == "video"
        and media.video_path
        and not (media.carousel_image_paths or media.keyframe_paths)
    ):
        fps = (
            settings.short_video_fps
            if media.duration_seconds <= settings.short_video_max_seconds
            else settings.video_fps
        )
        video = VideoInput(path=media.video_path, fps=fps)

    llm = app_settings.llm
    return CompletionRequest(
        prompt=prompt,
        response_schema=CrusherAnalysis,
        images=images,
        video=video,
        model=llm.model,
        fallback_model=llm.fallback_model or settings.fallback_model,
    )


def estimate_tokens(prompt: str, media: AcquiredMedia, settings: CrusherSettings) -> int:
    """Pre-flight estimate for TPM pacing and the spend cap. Actuals come from usage_metadata."""
    text = len(prompt) // 4 + 400  # ~4 chars/token + schema overhead
    images = len(media.carousel_image_paths or media.keyframe_paths) * settings.estimated_tokens_per_image
    video = 0
    if settings.visual_mode == "video" and media.video_path and not images:
        video = int(max(media.duration_seconds, 1.0) * settings.video_fps * 100)
    return text + images + video


def analyze_media(
    budget: BudgetManager,
    runtime: LlmRuntime,
    settings: Settings,
    media: AcquiredMedia,
    ctx: AnalyzeContext,
    taxonomy_keys: list[str],
) -> CrusherAnalysis:
    """One summary call, retried on the next API key when the current key's daily quota is spent."""
    cs = settings.crusher
    prompt = build_prompt(ctx, media, cs, taxonomy_keys)
    tokens = estimate_tokens(prompt, media, cs)
    request = build_completion_request(prompt, media, cs, settings)

    def _once(key_var: str) -> CrusherAnalysis:
        budget.check_spend(estimated_input_tokens=tokens)
        budget.wait_for_slot(estimated_tokens=tokens)
        result = runtime.complete_on_key(request, key_var)
        budget.record_request(key_var, tokens=result.input_tokens or tokens)
        budget.record_gemini_cost(result.input_tokens or tokens, result.output_tokens)
        try:
            return CrusherAnalysis.model_validate_json(result.text or "")
        except ValidationError:
            raise

    return budget.call_rotating(_once)
