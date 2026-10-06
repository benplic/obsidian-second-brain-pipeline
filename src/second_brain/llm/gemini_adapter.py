"""Native Google Gemini adapter (schema, thinking, video file upload)."""

from __future__ import annotations

import logging
import mimetypes
import time
from pathlib import Path
from typing import TYPE_CHECKING

from google.genai import errors as genai_errors
from pydantic import BaseModel, ValidationError

from .types import (
    CompletionRequest,
    CompletionResult,
    QuotaExhaustedError,
    RateLimitError,
    UnsupportedFeatureError,
)

if TYPE_CHECKING:
    from ..config import CrusherSettings, LlmSettings

logger = logging.getLogger(__name__)

# Cleared the first time a model rejects thinking_level, so later calls skip it.
_thinking_supported = True


def _map_api_error(exc: genai_errors.APIError) -> BaseException:
    if exc.code == 429:
        text = str(exc)
        if "PerDay" in text:
            return QuotaExhaustedError(
                "Daily quota exhausted; it resets at midnight Pacific. Re-run tomorrow."
            )
        return RateLimitError(str(exc))
    return exc


def _usage(response) -> tuple[int, int]:
    meta = getattr(response, "usage_metadata", None)
    if meta is None:
        return 0, 0
    prompt = int(getattr(meta, "prompt_token_count", 0) or 0)
    output = int(getattr(meta, "candidates_token_count", 0) or 0) + int(
        getattr(meta, "thoughts_token_count", 0) or 0
    )
    return prompt, output


def _image_mime(path: Path) -> str:
    return mimetypes.guess_type(path.name)[0] or "image/jpeg"


def _read_bytes(path: Path, max_mb: float) -> bytes | None:
    if not path.is_file():
        return None
    if path.stat().st_size / (1024 * 1024) > max_mb:
        return None
    return path.read_bytes()


def _upload_file(client, path: Path, crusher: CrusherSettings, mime: str):
    from google.genai import types

    uploaded = client.files.upload(file=path, config=types.UploadFileConfig(mime_type=mime))
    deadline = time.time() + crusher.file_poll_max_wait_seconds
    while time.time() < deadline:
        current = client.files.get(name=uploaded.name)
        state_name = getattr(getattr(current, "state", None), "name", "")
        if state_name == "ACTIVE":
            return current
        if state_name == "FAILED":
            raise RuntimeError(f"Gemini file processing failed for {path.name}")
        time.sleep(crusher.file_poll_seconds)
    raise TimeoutError(f"Timed out waiting for Gemini file {path.name}")


def _resolve_media_resolution(crusher: CrusherSettings | None):
    from google.genai import types

    raw = crusher.media_resolution if crusher else "MEDIA_RESOLUTION_LOW"
    if isinstance(raw, str) and hasattr(types.MediaResolution, raw):
        return getattr(types.MediaResolution, raw)
    return types.MediaResolution.MEDIA_RESOLUTION_LOW


class GeminiAdapter:
    """google-genai generate_content with optional crush-specific media handling."""

    name = "gemini"

    def __init__(self, llm: LlmSettings, crusher: CrusherSettings | None = None):
        self._llm = llm
        self._crusher = crusher

    @property
    def provider_label(self) -> str:
        return "gemini"

    def _client(self, api_key: str):
        from google import genai

        return genai.Client(api_key=api_key.strip())

    def _build_parts(self, client, request: CompletionRequest) -> list:
        from google.genai import types

        parts: list = [request.prompt]
        resolution = _resolve_media_resolution(self._crusher)
        inline_max = self._crusher.inline_max_mb if self._crusher else 18.0
        max_images = self._llm.max_images
        images = list(request.images)
        if request.extra_image_objects:
            import io

            from .types import ImageInput

            for obj in request.extra_image_objects:
                buf = io.BytesIO()
                obj.save(buf, format="JPEG")
                images.append(ImageInput(data=buf.getvalue(), mime_type="image/jpeg"))

        if max_images is not None and len(images) > max_images:
            images = images[:max_images]

        for idx, img in enumerate(images):
            data = img.data
            if data is None and img.path is not None:
                data = _read_bytes(img.path, inline_max)
            if not data:
                continue
            mime = img.mime_type or (_image_mime(img.path) if img.path else "image/jpeg")
            parts.append(types.Part.from_bytes(data=data, mime_type=mime, media_resolution=resolution))
            if img.caption:
                parts.append(img.caption)
            elif len(images) > 1:
                parts.append(f"Image {idx + 1} of {len(images)}.")

        if request.audio:
            audio_bytes, mime = request.audio
            parts.append(types.Part.from_bytes(data=audio_bytes, mime_type=mime))

        if request.video:
            if not self._crusher:
                raise UnsupportedFeatureError(
                    "Video upload requires native Gemini crush settings (crusher.visual_mode: video)."
                )
            mime = request.video.mime_type
            fps = request.video.fps
            data = _read_bytes(request.video.path, inline_max)
            if data:
                parts.append(
                    types.Part(
                        inline_data=types.Blob(data=data, mime_type=mime),
                        media_resolution=resolution,
                        video_metadata=types.VideoMetadata(fps=fps),
                    )
                )
            else:
                uploaded = _upload_file(client, request.video.path, self._crusher, mime)
                parts.append(
                    types.Part(
                        file_data=types.FileData(file_uri=uploaded.uri, mime_type=mime),
                        media_resolution=resolution,
                        video_metadata=types.VideoMetadata(fps=fps),
                    )
                )
        return parts

    def _config(self, schema: type[BaseModel] | None):
        from google.genai import types

        global _thinking_supported
        kwargs: dict = {}
        mode = self._llm.effective_structured_output()
        if schema is not None and mode in ("schema", None):
            kwargs["response_mime_type"] = "application/json"
            kwargs["response_schema"] = schema
        elif mode == "json_object":
            kwargs["response_mime_type"] = "application/json"
        if self._crusher:
            kwargs["media_resolution"] = _resolve_media_resolution(self._crusher)
        if _thinking_supported and self._llm.thinking_level:
            level = getattr(types.ThinkingLevel, str(self._llm.thinking_level).upper(), None)
            if level is not None:
                kwargs["thinking_config"] = types.ThinkingConfig(thinking_level=level)
        return types.GenerateContentConfig(**kwargs) if kwargs else None

    def complete(self, request: CompletionRequest, *, api_key: str) -> CompletionResult:
        if request.images and not self._llm.supports_vision:
            raise UnsupportedFeatureError("llm.supports_vision is false but images were attached.")
        if request.audio and not self._llm.supports_audio:
            raise UnsupportedFeatureError("llm.supports_audio is false but audio was attached.")
        if request.video and not self._llm.supports_video_upload:
            raise UnsupportedFeatureError(
                "Video upload is only supported on native Gemini with crusher.visual_mode: video."
            )

        client = self._client(api_key)
        model = request.model or self._llm.model
        fallback = request.fallback_model or self._llm.fallback_model
        parts = self._build_parts(client, request)
        config = self._config(request.response_schema)

        def _generate(use_model: str):
            global _thinking_supported
            last_exc: genai_errors.APIError | None = None
            base = self._llm.backoff_base_seconds
            for attempt in range(3):
                try:
                    return client.models.generate_content(
                        model=use_model, contents=parts, config=config
                    )
                except genai_errors.APIError as exc:
                    if exc.code == 400 and _thinking_supported and "thinking" in str(exc).lower():
                        logger.warning("Model %s rejected thinking_level; retrying without it.", use_model)
                        _thinking_supported = False
                        config = self._config(request.response_schema)
                        continue
                    if exc.code not in {500, 503} or attempt == 2:
                        raise _map_api_error(exc) from exc
                    last_exc = exc
                    delay = base * (attempt + 1)
                    logger.warning("Gemini %s (attempt %d/3). Waiting %.0fs...", exc.code, attempt + 1, delay)
                    time.sleep(delay)
            raise last_exc  # pragma: no cover

        try:
            response = _generate(model)
        except genai_errors.APIError as exc:
            raise _map_api_error(exc) from exc

        in_tok, out_tok = _usage(response)
        text = response.text or ""
        if request.response_schema is not None:
            try:
                request.response_schema.model_validate_json(text)
            except ValidationError:
                if not fallback:
                    raise
                logger.warning("Primary model JSON invalid; trying fallback %s", fallback)
                response = _generate(fallback)
                in_tok, out_tok = _usage(response)
                text = response.text or ""
        return CompletionResult(text=text, input_tokens=in_tok, output_tokens=out_tok)
