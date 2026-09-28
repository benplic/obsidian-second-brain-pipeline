"""Config-driven Gemini quota and incremental API-key fallback.

Keys are discovered, not listed one by one. For each name in
``crusher.api_key_env_vars`` the crusher uses that variable and then
``NAME_2``, ``NAME_3``, ... for as many consecutive numbers as are set in the
environment. When a key's daily quota is exhausted the same request is retried
on the next key. The run stops only after the last key is exhausted.
"""

from __future__ import annotations

import logging
import os
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TypeVar

from ..config import CrusherSettings
from ..gemini import DailyQuotaExhaustedError, MissingApiKeyError

T = TypeVar("T")
_NUMBERED_SUFFIX = re.compile(r"_(\d+)$")
# A missing number ends the sequence, so a typo like GEMINI_API_KEY_4 with no
# _3 cannot silently skip a key. 100 is a backstop, not a supported key count.
_MAX_NUMBERED_KEYS = 100

logger = logging.getLogger(__name__)

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Windows without tzdata before install
    ZoneInfo = None  # type: ignore[misc, assignment]


def _pacific_midnight_utc(now: datetime | None = None) -> datetime:
    """Start of the current quota day in Pacific time, as UTC."""
    now = now or datetime.now(timezone.utc)
    if ZoneInfo is None:
        # Fallback: treat UTC midnight as quota boundary when tzdata is missing.
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    pacific = now.astimezone(ZoneInfo("America/Los_Angeles"))
    start_local = pacific.replace(hour=0, minute=0, second=0, microsecond=0)
    return start_local.astimezone(timezone.utc)


class SpendCapReachedError(RuntimeError):
    """The next call would exceed crusher.max_spend_usd / max_total_spend_usd."""


# Output tokens assumed per summary for the pre-flight spend check. Real
# output (including thinking tokens) is recorded after the call.
_EST_OUTPUT_TOKENS = 900


@dataclass
class CostLedger:
    """USD spend for this run plus a lifetime total persisted in the usage file."""

    run_usd: float = 0.0
    lifetime_usd: float = 0.0
    gemini_input_tokens: int = 0
    gemini_output_tokens: int = 0
    jev_input_tokens: int = 0
    gemini_calls: int = 0
    jev_calls: int = 0


@dataclass
class KeyUsage:
    requests_today: int = 0
    day_start_utc: datetime = field(default_factory=_pacific_midnight_utc)
    exhausted: bool = False


def discover_key_env_vars(configured: tuple[str, ...], environ: Mapping[str, str] | None = None) -> list[str]:
    """Ordered env-var names that hold a non-empty key.

    ``("GEMINI_API_KEY",)`` plus ``GEMINI_API_KEY_2`` and ``GEMINI_API_KEY_3``
    in the environment yields those three, in that order. The sequence for a
    base name stops at the first missing number.
    """
    env = os.environ if environ is None else environ

    def present(name: str) -> bool:
        return bool((env.get(name) or "").strip())

    ordered: list[str] = []
    seen: set[str] = set()
    seen_values: set[str] = set()

    def add(name: str) -> None:
        value = (env.get(name) or "").strip()
        # The same secret under two names is one quota, not a fallback.
        if not value or name in seen or value in seen_values:
            return
        seen.add(name)
        seen_values.add(value)
        ordered.append(name)

    expanded_bases: set[str] = set()
    for name in configured:
        match = _NUMBERED_SUFFIX.search(name)
        base = name[: match.start()] if match else name
        if base in expanded_bases:
            add(name)
            continue
        expanded_bases.add(base)
        add(base)
        number = 2
        while present(f"{base}_{number}") and number <= _MAX_NUMBERED_KEYS:
            add(f"{base}_{number}")
            number += 1
        # An explicitly configured name that is not in the numbered run (odd, but honored).
        add(name)
    return ordered


class BudgetManager:
    """Tracks RPM/RPD/TPM and rotates API keys from the environment."""

    def __init__(self, settings: CrusherSettings, usage_path: Path):
        self.settings = settings
        self.usage_path = Path(usage_path)
        # Per key, so falling over to the next key does not inherit this key's minute window.
        self._minute_requests: dict[str, list[float]] = {}
        self._minute_tokens: dict[str, list[tuple[float, int]]] = {}
        self._key_usage: dict[str, KeyUsage] = {}
        self._key_index = 0
        self.cost = CostLedger()
        self._price_warning_logged = False
        self._load_usage_file()

    def _load_usage_file(self) -> None:
        if not self.usage_path.is_file():
            return
        try:
            import json

            data = json.loads(self.usage_path.read_text(encoding="utf-8"))
            for key_name, raw in (data.get("keys") or {}).items():
                self._key_usage[key_name] = KeyUsage(
                    requests_today=int(raw.get("requests_today", 0)),
                    day_start_utc=datetime.fromisoformat(raw["day_start_utc"]),
                    exhausted=bool(raw.get("exhausted", False)),
                )
            self.cost.lifetime_usd = float((data.get("cost") or {}).get("lifetime_usd", 0.0))
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            logger.warning("Could not load crusher usage file (%s); starting fresh.", exc)

    def _persist_usage(self) -> None:
        import json

        payload = {
            "keys": {
                name: {
                    "requests_today": usage.requests_today,
                    "day_start_utc": usage.day_start_utc.isoformat(),
                    "exhausted": usage.exhausted,
                }
                for name, usage in self._key_usage.items()
            },
            "cost": {"lifetime_usd": round(self.cost.lifetime_usd, 6)},
        }
        self.usage_path.parent.mkdir(parents=True, exist_ok=True)
        self.usage_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def _reset_day_if_needed(self, usage: KeyUsage) -> None:
        if datetime.now(timezone.utc) >= usage.day_start_utc + timedelta(days=1):
            usage.requests_today = 0
            usage.exhausted = False
            usage.day_start_utc = _pacific_midnight_utc()

    def available_keys(self) -> list[str]:
        return discover_key_env_vars(self.settings.api_key_env_vars)

    def retest_exhausted_keys(self) -> list[str]:
        """Clear same-day ``exhausted`` flags once per run so the next request confirms them.

        A flag from an earlier run (or a stale write) would otherwise skip the key until
        midnight Pacific. The first real request is the retest: a daily-quota response
        marks the key exhausted again and the crusher moves to the next key. Keys that
        already hit ``requests_per_day`` stay exhausted; that cap is config, not a guess.
        A new Pacific day is cleared by ``_reset_day_if_needed`` before this check.
        """
        cleared: list[str] = []
        daily_cap = self.settings.requests_per_day
        for var in self.available_keys():
            usage = self._key_usage.setdefault(var, KeyUsage())
            self._reset_day_if_needed(usage)
            if not usage.exhausted:
                continue
            if daily_cap is not None and usage.requests_today >= daily_cap:
                continue
            usage.exhausted = False
            cleared.append(var)
        if cleared:
            # Start the run on the first key again so the cleared flag is actually retested.
            self._key_index = 0
            logger.info(
                "Retesting %d key(s) marked exhausted earlier: %s. "
                "A daily-quota response on the next request marks that key again.",
                len(cleared),
                ", ".join(cleared),
            )
            self._persist_usage()
        return cleared

    def current_key_var(self) -> str:
        keys = self.available_keys()
        if not keys:
            raise MissingApiKeyError(
                "No Gemini API keys found. Set one of: "
                + ", ".join(self.settings.api_key_env_vars)
            )
        for offset in range(len(keys)):
            idx = (self._key_index + offset) % len(keys)
            var = keys[idx]
            usage = self._key_usage.setdefault(var, KeyUsage())
            self._reset_day_if_needed(usage)
            if usage.exhausted:
                continue
            if self.settings.requests_per_day is not None and usage.requests_today >= self.settings.requests_per_day:
                usage.exhausted = True
                continue
            self._key_index = idx
            return var
        names = ", ".join(keys)
        raise DailyQuotaExhaustedError(
            f"All {len(keys)} Gemini API keys are exhausted for today ({names}). "
            "Re-run after midnight Pacific, or add GEMINI_API_KEY_<n> and re-run."
        )

    def create_client(self):
        """Client bound to the current fallback key, plus that key's env-var name."""
        var = self.current_key_var()
        return self.client_for(var), var

    def client_for(self, key_var: str):
        """Client for one already-chosen key. The secret is passed in, not copied into the environment."""
        from ..gemini import create_client

        return create_client(api_key=os.environ[key_var])

    def call_rotating(self, call: Callable[[str], T]) -> T:
        """Run ``call(key_var)``. On a daily-quota error, retire that key and retry on the next.

        Per-minute 429s are not switched here; ``call_with_backoff`` already waits those out
        on the current key. This walks the discovered keys in order and raises
        ``DailyQuotaExhaustedError`` once the last key is spent.
        """
        while True:
            var = self.current_key_var()
            try:
                return call(var)
            except DailyQuotaExhaustedError as exc:
                self.mark_daily_exhausted(var)
                remaining = self._ready_keys()
                if not remaining:
                    raise DailyQuotaExhaustedError(
                        f"All {len(self.available_keys())} Gemini API keys are exhausted for today. "
                        "Re-run after midnight Pacific, or add another GEMINI_API_KEY_<n>."
                    ) from exc
                logger.warning(
                    "%s daily quota exhausted. Switching to %s (%d key(s) left).",
                    var,
                    remaining[0],
                    len(remaining),
                )

    def _ready_keys(self) -> list[str]:
        ready: list[str] = []
        for var in self.available_keys():
            usage = self._key_usage.setdefault(var, KeyUsage())
            self._reset_day_if_needed(usage)
            if usage.exhausted:
                continue
            if self.settings.requests_per_day is not None and usage.requests_today >= self.settings.requests_per_day:
                continue
            ready.append(var)
        return ready

    def _prune_minute_windows(self, key_var: str, now: float) -> None:
        cutoff = now - 60.0
        self._minute_requests[key_var] = [t for t in self._minute_requests.get(key_var, []) if t >= cutoff]
        self._minute_tokens[key_var] = [(t, n) for t, n in self._minute_tokens.get(key_var, []) if t >= cutoff]

    def wait_for_slot(self, *, estimated_tokens: int, sleep=time.sleep) -> None:
        """Block until RPM/RPD/TPM allow one request on the current key."""
        while True:
            key_var = self.current_key_var()
            usage = self._key_usage.setdefault(key_var, KeyUsage())
            self._reset_day_if_needed(usage)
            now = time.time()
            self._prune_minute_windows(key_var, now)
            requests = self._minute_requests.setdefault(key_var, [])
            tokens = self._minute_tokens.setdefault(key_var, [])
            rpm = self.settings.requests_per_minute
            tpm = self.settings.tokens_per_minute
            if rpm is not None and len(requests) >= rpm:
                sleep(max(0.5, 60.0 - (now - requests[0]) + 0.1))
                continue
            if tpm is not None:
                used = sum(n for _, n in tokens)
                if used + estimated_tokens > tpm:
                    sleep(1.0)
                    continue
            return

    def record_request(self, key_var: str, *, tokens: int) -> None:
        usage = self._key_usage.setdefault(key_var, KeyUsage())
        self._reset_day_if_needed(usage)
        usage.requests_today += 1
        now = time.time()
        self._minute_requests.setdefault(key_var, []).append(now)
        self._minute_tokens.setdefault(key_var, []).append((now, tokens))
        self._persist_usage()

    # -- cost ledger --------------------------------------------------------

    def _prices_known(self) -> bool:
        return self.settings.price_input_per_m is not None and self.settings.price_output_per_m is not None

    def _gemini_usd(self, input_tokens: int, output_tokens: int) -> float:
        if not self._prices_known():
            return 0.0
        return (
            input_tokens * float(self.settings.price_input_per_m)
            + output_tokens * float(self.settings.price_output_per_m)
        ) / 1_000_000

    def _add_usd(self, usd: float) -> None:
        self.cost.run_usd += usd
        self.cost.lifetime_usd += usd

    def record_gemini_cost(self, input_tokens: int, output_tokens: int) -> None:
        self.cost.gemini_calls += 1
        self.cost.gemini_input_tokens += int(input_tokens)
        self.cost.gemini_output_tokens += int(output_tokens)
        self._add_usd(self._gemini_usd(int(input_tokens), int(output_tokens)))
        self._persist_usage()

    def record_jev_usage(self, input_tokens: int) -> None:
        # Jev output is free; only input is billed.
        self.cost.jev_calls += 1
        self.cost.jev_input_tokens += int(input_tokens)
        self._add_usd(int(input_tokens) * float(self.settings.jev_price_input_per_m) / 1_000_000)
        self._persist_usage()

    def check_spend(self, *, estimated_input_tokens: int) -> None:
        """Raise SpendCapReachedError if the next Gemini call could cross a cap."""
        caps = [
            ("crusher.max_spend_usd (this run)", self.settings.max_spend_usd, self.cost.run_usd),
            ("crusher.max_total_spend_usd (lifetime)", self.settings.max_total_spend_usd, self.cost.lifetime_usd),
        ]
        if not any(cap is not None for _, cap, _ in caps):
            return
        if not self._prices_known():
            if not self._price_warning_logged:
                self._price_warning_logged = True
                logger.warning(
                    "A spend cap is set but crusher.price_input_per_m / price_output_per_m are not. "
                    "Spend cannot be measured, so the cap is NOT enforced. Set both prices for your model."
                )
            return
        projected = self._gemini_usd(estimated_input_tokens, _EST_OUTPUT_TOKENS)
        for label, cap, spent in caps:
            if cap is not None and spent + projected > float(cap):
                raise SpendCapReachedError(
                    f"{label} ${float(cap):.2f} reached (spent ${spent:.4f}, next call ~${projected:.4f})."
                )

    def spend_summary(self) -> str:
        c = self.cost
        priced = "" if self._prices_known() else " (Gemini prices not set: Gemini $ shown as 0)"
        return (
            f"spend this run ${c.run_usd:.4f}, lifetime ${c.lifetime_usd:.4f}{priced}; "
            f"gemini calls={c.gemini_calls} in={c.gemini_input_tokens} out={c.gemini_output_tokens}; "
            f"jev calls={c.jev_calls} in={c.jev_input_tokens}"
        )

    def mark_daily_exhausted(self, key_var: str) -> None:
        usage = self._key_usage.setdefault(key_var, KeyUsage())
        usage.exhausted = True
        self._persist_usage()
        self._key_index += 1
