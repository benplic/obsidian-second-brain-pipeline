"""Backoff + key rotation wrapper used by categorize and model-a."""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..gemini import call_with_backoff
from .keys import KeyPool
from .types import CompletionRequest, CompletionResult, QuotaExhaustedError, RateLimitError

if TYPE_CHECKING:
    from ..config import LlmSettings, Settings

logger = logging.getLogger(__name__)


@dataclass
class LlmRuntime:
    """Single entry point for pipelines: rotate keys on quota, backoff on rate limits."""

    settings: Settings
    adapter: object
    key_pool: KeyPool

    @property
    def llm(self) -> LlmSettings:
        return self.settings.llm

    def complete(
        self,
        request: CompletionRequest,
        *,
        sleep: Callable[[float], None] = time.sleep,
        rng: Callable[[float, float], float] = random.uniform,
    ) -> CompletionResult:
        cfg = self.llm

        def once(key_var: str) -> CompletionResult:
            api_key = self.key_pool.api_key_for(key_var)

            def _call() -> CompletionResult:
                try:
                    return self.adapter.complete(request, api_key=api_key)
                except RateLimitError:
                    raise
                except QuotaExhaustedError:
                    raise

            return call_with_backoff(
                _call,
                max_retries=cfg.max_retries,
                base_seconds=cfg.backoff_base_seconds,
                jitter=cfg.jitter_seconds,
                sleep=sleep,
                rng=rng,
            )

        return self.key_pool.call(once)

    def complete_on_key(
        self,
        request: CompletionRequest,
        key_var: str,
        *,
        sleep: Callable[[float], None] = time.sleep,
        rng: Callable[[float, float], float] = random.uniform,
    ) -> CompletionResult:
        """One key only (crush budget manager rotates externally)."""

        api_key = self.key_pool.api_key_for(key_var)

        def _call() -> CompletionResult:
            return self.adapter.complete(request, api_key=api_key)

        return call_with_backoff(
            _call,
            max_retries=self.llm.max_retries,
            base_seconds=self.llm.backoff_base_seconds,
            jitter=self.llm.jitter_seconds,
            sleep=sleep,
            rng=rng,
        )
