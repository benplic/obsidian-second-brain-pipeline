"""OpenAI-compatible chat completions (OpenAI, OpenRouter, Groq, etc.)."""

from __future__ import annotations

import base64
import json
import logging
from typing import TYPE_CHECKING

from pydantic import BaseModel, ValidationError

from .types import (
    CompletionRequest,
    CompletionResult,
    QuotaExhaustedError,
    RateLimitError,
    UnsupportedFeatureError,
)

if TYPE_CHECKING:
    from ..config import LlmSettings

logger = logging.getLogger(__name__)
_thinking_warned = False


class OpenAICompatAdapter:
    name = "openai"

    def __init__(self, llm: LlmSettings):
        self._llm = llm

    @property
    def provider_label(self) -> str:
        return "openai-compatible"

    def _client(self, api_key: str):
        from openai import OpenAI

        return OpenAI(api_key=api_key.strip(), base_url=self._llm.base_url)

    def _map_error(self, exc: Exception) -> BaseException:
        from openai import APIStatusError

        if not isinstance(exc, APIStatusError):
            return exc
        if exc.status_code != 429:
            return exc
        body = str(exc).lower()
        if "insufficient_quota" in body or "exceeded your current quota" in body:
            return QuotaExhaustedError(str(exc))
        if "rate_limit" in body:
            return RateLimitError(str(exc))
        # Unknown 429: backoff on same key, do not rotate.
        return RateLimitError(str(exc))

    def _image_url(self, data: bytes, mime: str) -> str:
        encoded = base64.b64encode(data).decode("ascii")
        return f"data:{mime};base64,{encoded}"

    def _messages(self, request: CompletionRequest) -> list[dict]:
        content: list[dict] = [{"type": "text", "text": request.prompt}]
        max_images = self._llm.max_images
        images = list(request.images)
        if request.extra_image_objects:
            import io

            from .types import ImageInput

            for obj in request.extra_image_objects:
                buf = io.BytesIO()
                obj.save(buf, format="JPEG")
                images.append(ImageInput(data=buf.getvalue(), mime_type="image/jpeg"))
        if max_images is not None:
            images = images[:max_images]
        for img in images:
            data = img.data
            if data is None and img.path is not None and img.path.is_file():
                data = img.path.read_bytes()
            if not data:
                continue
            mime = img.mime_type or "image/jpeg"
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": self._image_url(data, mime)},
                }
            )
        if request.audio:
            raise UnsupportedFeatureError(
                "Audio transcription tier requires native Gemini (llm.provider: gemini) with supports_audio."
            )
        if request.video:
            raise UnsupportedFeatureError(
                "Raw video requires native Gemini (crusher.visual_mode: video). Use visual_mode: frames."
            )
        return [{"role": "user", "content": content}]

    def _response_format(self, schema: type[BaseModel] | None) -> dict | None:
        mode = self._llm.effective_structured_output()
        if schema is not None and mode in ("schema", None):
            return {
                "type": "json_schema",
                "json_schema": {
                    "name": schema.__name__,
                    "schema": schema.model_json_schema(),
                    "strict": True,
                },
            }
        if mode in ("json_object", "schema"):
            return {"type": "json_object"}
        return None

    def complete(self, request: CompletionRequest, *, api_key: str) -> CompletionResult:
        global _thinking_warned
        if self._llm.thinking_level and not _thinking_warned:
            _thinking_warned = True
            logger.info("thinking_level is ignored for OpenAI-compatible providers.")

        if request.images and not self._llm.supports_vision:
            raise UnsupportedFeatureError("llm.supports_vision is false but images were attached.")

        client = self._client(api_key)
        model = request.model or self._llm.model
        messages = self._messages(request)
        response_format = self._response_format(request.response_schema)

        def _call(extra_user: str | None = None) -> CompletionResult:
            msgs = list(messages)
            if extra_user:
                msgs = msgs + [{"role": "user", "content": extra_user}]
            kwargs: dict = {"model": model, "messages": msgs}
            if response_format:
                kwargs["response_format"] = response_format
            try:
                resp = client.chat.completions.create(**kwargs)
            except Exception as exc:
                mapped = self._map_error(exc)
                if mapped is not exc:
                    raise mapped
                raise
            choice = resp.choices[0].message
            text = (choice.content or "").strip()
            usage = resp.usage
            in_tok = int(usage.prompt_tokens if usage else 0)
            out_tok = int(usage.completion_tokens if usage else 0)
            return CompletionResult(text=text, input_tokens=in_tok, output_tokens=out_tok)

        result = _call()
        if request.response_schema is None:
            return result
        try:
            request.response_schema.model_validate_json(result.text)
            return result
        except ValidationError:
            mode = self._llm.effective_structured_output()
            if mode != "prompt_only":
                # One repair turn for json_object / failed schema.
                repair = (
                    "Your previous reply was not valid JSON for the required schema. "
                    "Reply with ONLY valid JSON, no markdown.\n"
                    f"Schema hint: {json.dumps(request.response_schema.model_json_schema())}"
                )
                repaired = _call(extra_user=repair)
                request.response_schema.model_validate_json(repaired.text)
                return repaired
            raise
