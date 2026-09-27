"""Config-driven Gemini quota and multi-key rotation."""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..config import CrusherSettings
from ..gemini import DailyQuotaExhaustedError, MissingApiKeyError

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


@dataclass
class KeyUsage:
    requests_today: int = 0
    day_start_utc: datetime = field(default_factory=_pacific_midnight_utc)
    exhausted: bool = False


class BudgetManager:
    """Tracks RPM/RPD/TPM and rotates API keys from the environment."""

    def __init__(self, settings: CrusherSettings, usage_path: Path):
        self.settings = settings
        self.usage_path = Path(usage_path)
        self._minute_requests: list[float] = []
        self._minute_tokens: list[tuple[float, int]] = []
        self._key_usage: dict[str, KeyUsage] = {}
        self._key_index = 0
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
            }
        }
        self.usage_path.parent.mkdir(parents=True, exist_ok=True)
        self.usage_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def _reset_day_if_needed(self, usage: KeyUsage) -> None:
        if datetime.now(timezone.utc) >= usage.day_start_utc + timedelta(days=1):
            usage.requests_today = 0
            usage.exhausted = False
            usage.day_start_utc = _pacific_midnight_utc()

    def available_keys(self) -> list[str]:
        keys: list[str] = []
        for var in self.settings.api_key_env_vars:
            value = os.environ.get(var)
            if value and value.strip():
                keys.append(var)
        return keys

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
        raise DailyQuotaExhaustedError(
            "All configured Gemini API keys hit their daily quota (crusher.requests_per_day). Re-run after reset."
        )

    def create_client(self):
        from ..gemini import create_client

        var = self.current_key_var()
        previous = os.environ.get("GEMINI_API_KEY")
        os.environ["GEMINI_API_KEY"] = os.environ[var]
        try:
            return create_client(), var
        finally:
            if previous is None:
                os.environ.pop("GEMINI_API_KEY", None)
            else:
                os.environ["GEMINI_API_KEY"] = previous

    def _prune_minute_windows(self, now: float) -> None:
        cutoff = now - 60.0
        self._minute_requests = [t for t in self._minute_requests if t >= cutoff]
        self._minute_tokens = [(t, n) for t, n in self._minute_tokens if t >= cutoff]

    def estimate_tokens(self, *, duration_seconds: float, fps: float, image_count: int = 0) -> int:
        # Rough heuristic for pre-flight TPM throttling (video ~300 tok/s sampled + images).
        video_tokens = int(max(duration_seconds, 1.0) * fps * 300)
        return video_tokens + image_count * 600 + 800

    def wait_for_slot(self, *, estimated_tokens: int, sleep=time.sleep) -> None:
        """Block until RPM/RPD/TPM allow one request on the current key."""
        while True:
            key_var = self.current_key_var()
            usage = self._key_usage.setdefault(key_var, KeyUsage())
            self._reset_day_if_needed(usage)
            now = time.time()
            self._prune_minute_windows(now)
            rpm = self.settings.requests_per_minute
            tpm = self.settings.tokens_per_minute
            if rpm is not None and len(self._minute_requests) >= rpm:
                sleep(max(0.5, 60.0 - (now - self._minute_requests[0]) + 0.1))
                continue
            if tpm is not None:
                used = sum(n for _, n in self._minute_tokens)
                if used + estimated_tokens > tpm:
                    sleep(1.0)
                    continue
            return

    def record_request(self, key_var: str, *, tokens: int) -> None:
        usage = self._key_usage.setdefault(key_var, KeyUsage())
        self._reset_day_if_needed(usage)
        usage.requests_today += 1
        now = time.time()
        self._minute_requests.append(now)
        self._minute_tokens.append((now, tokens))
        self._persist_usage()

    def mark_daily_exhausted(self, key_var: str) -> None:
        usage = self._key_usage.setdefault(key_var, KeyUsage())
        usage.exhausted = True
        self._persist_usage()
        self._key_index += 1
