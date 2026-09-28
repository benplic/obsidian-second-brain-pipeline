"""Crusher budget and key rotation."""

from __future__ import annotations

import os

import pytest

from second_brain.config import CrusherSettings
from second_brain.crusher.budget import BudgetManager, SpendCapReachedError
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


def test_spend_cap_blocks_next_call(tmp_path):
    settings = CrusherSettings(price_input_per_m=1.0, price_output_per_m=4.0, max_spend_usd=0.01)
    budget = BudgetManager(settings, tmp_path / "usage.json")
    budget.check_spend(estimated_input_tokens=1000)  # ~$0.0046 projected: fine
    budget.record_gemini_cost(5000, 500)  # $0.005 + $0.002 = $0.007
    with pytest.raises(SpendCapReachedError):
        budget.check_spend(estimated_input_tokens=1000)


def test_lifetime_cap_persists_across_runs(tmp_path):
    settings = CrusherSettings(price_input_per_m=1.0, price_output_per_m=1.0, max_total_spend_usd=0.01)
    first = BudgetManager(settings, tmp_path / "usage.json")
    first.record_gemini_cost(9000, 0)
    second = BudgetManager(settings, tmp_path / "usage.json")
    assert second.cost.lifetime_usd == pytest.approx(0.009)
    assert second.cost.run_usd == 0.0
    with pytest.raises(SpendCapReachedError):
        second.check_spend(estimated_input_tokens=2000)


def test_cap_without_prices_warns_and_does_not_block(tmp_path, caplog):
    budget = BudgetManager(CrusherSettings(max_spend_usd=0.0), tmp_path / "usage.json")
    budget.check_spend(estimated_input_tokens=10**7)
    assert "NOT enforced" in caplog.text


def test_jev_usage_priced_input_only(tmp_path):
    budget = BudgetManager(CrusherSettings(jev_price_input_per_m=0.042), tmp_path / "usage.json")
    budget.record_jev_usage(1_000_000)
    assert budget.cost.run_usd == pytest.approx(0.042)
    assert "jev calls=1" in budget.spend_summary()


def test_budget_raises_when_all_keys_exhausted(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "only")
    settings = CrusherSettings(api_key_env_vars=("GEMINI_API_KEY",), requests_per_day=1)
    budget = BudgetManager(settings, tmp_path / "usage.json")
    var = budget.current_key_var()
    budget.record_request(var, tokens=50)
    budget.mark_daily_exhausted(var)
    with pytest.raises(DailyQuotaExhaustedError):
        budget.current_key_var()
