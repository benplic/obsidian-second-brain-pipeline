"""Gemini client creation and rate-limit handling.

Free tier invariants (see README):
  * ~20 requests/day, reset at midnight Pacific (not a rolling window).
  * Per-minute limits return 429 / RESOURCE_EXHAUSTED -> exponential backoff + jitter.
"""

from __future__ import annotations

import logging
import os
import random
import time
from typing import Callable, TypeVar

from google.genai import errors as genai_errors

from .llm.types import QuotaExhaustedError, RateLimitError

logger = logging.getLogger(__name__)

T = TypeVar("T")

API_KEY_ENV_VARS = ("GEMINI_API_KEY", "GOOGLE_API_KEY")


class MissingApiKeyError(RuntimeError):
    """No Gemini API key in the environment or .env."""


class RateLimitExhaustedError(RuntimeError):
    """Still rate limited after all retries."""


class DailyQuotaExhaustedError(RuntimeError):
    """The per-day quota is spent; retrying today only burns more requests."""


def create_client(api_key: str | None = None):
    """Build a google-genai client.

    Pass ``api_key`` when the caller has already chosen a fallback key
    (``GEMINI_API_KEY_2``, ...). Otherwise the first of ``GEMINI_API_KEY`` /
    ``GOOGLE_API_KEY`` that is set is used. Created lazily so ``--help`` does
    not require a key.
    """
    from google import genai

    if api_key and api_key.strip():
        return genai.Client(api_key=api_key.strip())
    for var in API_KEY_ENV_VARS:
        key = os.environ.get(var)
        if key:
            logger.debug("Using Gemini API key from %s", var)
            return genai.Client(api_key=key)
    raise MissingApiKeyError(
        "No Gemini API key found. Set GEMINI_API_KEY in your environment or in a .env file "
        "(copy .env.example to .env)."
    )


def is_rate_limit_error(exc: BaseException) -> bool:
    if isinstance(exc, genai_errors.APIError) and exc.code == 429:
        return True
    # String check kept from the originals in case a transport wraps the error.
    text = str(exc)
    return "429" in text or "RESOURCE_EXHAUSTED" in text


def is_daily_quota_error(exc: BaseException) -> bool:
    """Heuristic: Google's 429 body names the violated quota, e.g.
    ``GenerateRequestsPerDayPerProjectPerModel-FreeTier``.

    TODO: Confirm against a real free-tier daily-limit response; if Google
    changes the wording this degrades gracefully to normal backoff.
    """
    return is_rate_limit_error(exc) and "PerDay" in str(exc)


def backoff_delay(attempt: int, base_seconds: float, jitter: tuple[float, float], rng: Callable[[float, float], float]) -> float:
    """base * 2^attempt + uniform(jitter). attempt is 0-based."""
    return base_seconds * (2 ** attempt) + rng(*jitter)


def call_with_backoff(
    func: Callable[[], T],
    *,
    max_retries: int,
    base_seconds: float,
    jitter: tuple[float, float] = (1.0, 3.0),
    sleep: Callable[[float], None] | None = None,
    rng: Callable[[float, float], float] | None = None,
) -> T:
    """Call ``func`` and retry only on rate limiting.

    Non-rate-limit API errors propagate immediately (retrying a bad request
    wastes quota). ``sleep``/``rng`` are injectable for tests.

    Raises:
        DailyQuotaExhaustedError: daily quota hit; no retries attempted.
        RateLimitExhaustedError: still 429 after ``max_retries`` attempts.
    """
    # Resolved at call time (not as default args) so patching time.sleep works.
    sleep = sleep or time.sleep
    rng = rng or random.uniform
    last_exc: BaseException | None = None
    for attempt in range(max_retries):
        try:
            return func()
        except QuotaExhaustedError as exc:
            raise DailyQuotaExhaustedError(str(exc)) from exc
        except RateLimitError as exc:
            last_exc = exc
            if attempt == max_retries - 1:
                break
            delay = backoff_delay(attempt, base_seconds, jitter, rng)
            logger.warning("Rate limit hit (attempt %d/%d). Waiting %.1fs...", attempt + 1, max_retries, delay)
            sleep(delay)
            continue
        except genai_errors.APIError as exc:
            if not is_rate_limit_error(exc):
                raise
            if is_daily_quota_error(exc):
                raise DailyQuotaExhaustedError(
                    "Gemini daily quota exhausted; it resets at midnight Pacific. Re-run tomorrow."
                ) from exc
            last_exc = exc
            if attempt == max_retries - 1:
                break  # no point sleeping after the final attempt
            delay = backoff_delay(attempt, base_seconds, jitter, rng)
            logger.warning("Rate limit hit (attempt %d/%d). Waiting %.1fs...", attempt + 1, max_retries, delay)
            sleep(delay)
    raise RateLimitExhaustedError(f"Still rate limited after {max_retries} attempts") from last_exc
