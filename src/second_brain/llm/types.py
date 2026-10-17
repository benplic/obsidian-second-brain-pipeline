"""Shared types and errors for LLM adapters (BYOK)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

StructuredOutputMode = Literal["schema", "json_object", "prompt_only"]


class MissingApiKeyError(RuntimeError):
    """No API key in the configured environment variables."""


class RateLimitError(RuntimeError):
    """Per-minute or transient rate limit; retry on the same key with backoff."""


class QuotaExhaustedError(RuntimeError):
    """Daily or account quota spent; rotate to the next key if available."""


class UnsupportedFeatureError(RuntimeError):
    """Request needs vision/audio/video the active provider or config does not support."""


@dataclass(frozen=True)
class Capabilities:
    supports_vision: bool = True
    supports_audio: bool = True
    supports_video_upload: bool = False
    max_images: int | None = 16
    structured_output: StructuredOutputMode | None = None  # None = adapter default


@dataclass
class ImageInput:
    """One image for multimodal completion."""

    path: Path | None = None
    data: bytes | None = None
    mime_type: str = "image/jpeg"
    caption: str | None = None


@dataclass
class VideoInput:
    path: Path
    fps: float = 1.0
    mime_type: str = "video/mp4"


@dataclass
class CompletionRequest:
    prompt: str
    response_schema: type[BaseModel] | None = None
    images: list[ImageInput] = field(default_factory=list)
    audio: tuple[bytes, str] | None = None  # (bytes, mime_type)
    video: VideoInput | None = None
    model: str | None = None
    fallback_model: str | None = None
    # Model A ingest passes PIL images; adapter encodes them.
    extra_image_objects: list[Any] = field(default_factory=list)


@dataclass
class CompletionResult:
    text: str
    input_tokens: int = 0
    output_tokens: int = 0
