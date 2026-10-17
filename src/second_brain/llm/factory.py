"""Build adapter + runtime from Settings (hot path)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .gemini_adapter import GeminiAdapter
from .keys import KeyPool
from .openai_adapter import OpenAICompatAdapter
from .runtime import LlmRuntime

if TYPE_CHECKING:
    from ..config import Settings


def build_runtime(settings: Settings) -> LlmRuntime:
    llm = settings.llm
    pool = KeyPool(llm.api_key_env_vars)
    if llm.provider == "gemini":
        adapter = GeminiAdapter(llm, crusher=settings.crusher)
    elif llm.provider == "openai":
        adapter = OpenAICompatAdapter(llm)
    else:
        from ..config import ConfigError

        raise ConfigError(f"Unknown llm.provider: {llm.provider!r}")
    return LlmRuntime(settings=settings, adapter=adapter, key_pool=pool)
