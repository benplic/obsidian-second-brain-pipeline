"""BYOK LLM adapters (Gemini native + OpenAI-compatible hub).

Import ``build_runtime`` from ``second_brain.llm.factory`` to avoid import cycles
with ``second_brain.gemini``.
"""

from .keys import KeyPool, discover_key_env_vars
from .types import (
    CompletionRequest,
    CompletionResult,
    ImageInput,
    MissingApiKeyError,
    QuotaExhaustedError,
    RateLimitError,
    UnsupportedFeatureError,
    VideoInput,
)

__all__ = [
    "CompletionRequest",
    "CompletionResult",
    "ImageInput",
    "KeyPool",
    "MissingApiKeyError",
    "QuotaExhaustedError",
    "RateLimitError",
    "UnsupportedFeatureError",
    "VideoInput",
    "discover_key_env_vars",
]
