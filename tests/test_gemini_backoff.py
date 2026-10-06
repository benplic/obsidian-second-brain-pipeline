"""Exponential backoff with jitter on 429 / RESOURCE_EXHAUSTED."""

from __future__ import annotations

import pytest
from google.genai import errors as genai_errors

from second_brain.gemini import (
    DailyQuotaExhaustedError,
    RateLimitExhaustedError,
    backoff_delay,
    call_with_backoff,
    is_rate_limit_error,
)
from second_brain.llm.types import RateLimitError

from conftest import rate_limit_error


def _flaky(failures: list[BaseException], value="ok"):
    calls = {"n": 0}

    def func():
        calls["n"] += 1
        if failures:
            raise failures.pop(0)
        return value

    return func, calls


def test_retries_429_then_succeeds_with_exponential_delays():
    sleeps: list[float] = []
    func, calls = _flaky([rate_limit_error(), rate_limit_error()])
    result = call_with_backoff(func, max_retries=5, base_seconds=8, jitter=(1, 3), sleep=sleeps.append, rng=lambda a, b: a)
    assert result == "ok"
    assert calls["n"] == 3
    assert sleeps == [9.0, 17.0]  # 8*2^0+1, 8*2^1+1


def test_jitter_stays_within_bounds():
    for attempt in range(5):
        for _ in range(50):
            delay = backoff_delay(attempt, 8, (1, 3), __import__("random").uniform)
            assert 8 * 2 ** attempt + 1 <= delay <= 8 * 2 ** attempt + 3


def test_gives_up_after_max_retries_without_trailing_sleep():
    sleeps: list[float] = []
    func, calls = _flaky([rate_limit_error() for _ in range(3)])
    with pytest.raises(RateLimitExhaustedError):
        call_with_backoff(func, max_retries=3, base_seconds=1, sleep=sleeps.append, rng=lambda a, b: 0)
    assert calls["n"] == 3
    assert len(sleeps) == 2


def test_non_rate_limit_error_is_not_retried():
    sleeps: list[float] = []
    bad_request = genai_errors.ClientError(400, {"error": {"code": 400, "message": "bad", "status": "INVALID_ARGUMENT"}})
    func, calls = _flaky([bad_request])
    with pytest.raises(genai_errors.ClientError):
        call_with_backoff(func, max_retries=5, base_seconds=1, sleep=sleeps.append)
    assert calls["n"] == 1 and sleeps == []


def test_daily_quota_stops_immediately():
    sleeps: list[float] = []
    func, calls = _flaky([rate_limit_error(per_day=True)])
    with pytest.raises(DailyQuotaExhaustedError):
        call_with_backoff(func, max_retries=5, base_seconds=1, sleep=sleeps.append)
    assert calls["n"] == 1 and sleeps == []


def test_llm_rate_limit_error_is_retried():
    sleeps: list[float] = []
    calls = {"n": 0}

    def func():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RateLimitError("rpm")
        return "ok"

    result = call_with_backoff(func, max_retries=3, base_seconds=1, sleep=sleeps.append, rng=lambda a, b: 0)
    assert result == "ok"
    assert calls["n"] == 2


def test_rate_limit_detection():
    assert is_rate_limit_error(rate_limit_error())
    assert is_rate_limit_error(RuntimeError("RESOURCE_EXHAUSTED upstream"))
    assert not is_rate_limit_error(RuntimeError("boom"))
