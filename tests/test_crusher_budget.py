"""Crusher budget and key rotation."""

from __future__ import annotations

import os

import pytest

from second_brain.config import CrusherSettings
from second_brain.crusher.budget import BudgetManager
from second_brain.gemini import DailyQuotaExhaustedError


def test_budget_rotates_after_daily_exhaustion(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "key-one")
    monkeypatch.setenv("GEMINI_API_KEY_2", "key-two")
    settings = CrusherSettings(
        api_key_env_vars=("GEMINI_API_KEY", "GEMINI_API_KEY_2"),
        requests_per_day=1,
    )
    budget = BudgetManager(settings, tmp_path / "usage.json")
    first = budget.current_key_var()
    budget.record_request(first, tokens=100)
    budget.mark_daily_exhausted(first)
    second = budget.current_key_var()
    assert second != first


def test_budget_raises_when_all_keys_exhausted(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "only")
    settings = CrusherSettings(api_key_env_vars=("GEMINI_API_KEY",), requests_per_day=1)
    budget = BudgetManager(settings, tmp_path / "usage.json")
    var = budget.current_key_var()
    budget.record_request(var, tokens=50)
    budget.mark_daily_exhausted(var)
    with pytest.raises(DailyQuotaExhaustedError):
        budget.current_key_var()
