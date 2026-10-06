"""Key discovery and rotation."""

from __future__ import annotations

import pytest

from second_brain.llm.keys import KeyPool, discover_key_env_vars
from second_brain.llm.types import QuotaExhaustedError


def test_numbered_keys_and_gap_stops_sequence():
    env = {"GEMINI_API_KEY": "a", "GEMINI_API_KEY_2": "b", "GEMINI_API_KEY_4": "skipped-gap"}
    assert discover_key_env_vars(("GEMINI_API_KEY",), env) == ["GEMINI_API_KEY", "GEMINI_API_KEY_2"]


def test_duplicate_secret_is_one_quota():
    env = {"GEMINI_API_KEY": "same", "GOOGLE_API_KEY": "same", "GOOGLE_API_KEY_2": "other"}
    found = discover_key_env_vars(("GEMINI_API_KEY", "GOOGLE_API_KEY"), env)
    assert found == ["GEMINI_API_KEY", "GOOGLE_API_KEY_2"]


def test_key_pool_rotates_on_quota():
    env = {"GEMINI_API_KEY": "k1", "GEMINI_API_KEY_2": "k2"}
    pool = KeyPool(("GEMINI_API_KEY",))
    seen: list[str] = []

    def fn(var: str) -> str:
        seen.append(var)
        if var == "GEMINI_API_KEY":
            raise QuotaExhaustedError("day")
        return "ok"

    assert pool.call(fn, environ=env) == "ok"
    assert seen == ["GEMINI_API_KEY", "GEMINI_API_KEY_2"]


def test_last_key_quota_raises():
    pool = KeyPool(("GEMINI_API_KEY",))
    with pytest.raises(QuotaExhaustedError):
        pool.call(lambda _v: (_ for _ in ()).throw(QuotaExhaustedError("day")), environ={"GEMINI_API_KEY": "x"})
