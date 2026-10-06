"""Discover numbered API-key environment variables and rotate on quota exhaustion."""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Mapping
from typing import TypeVar

from .types import MissingApiKeyError, QuotaExhaustedError

T = TypeVar("T")

_NUMBERED_SUFFIX = re.compile(r"_(\d+)$")
_MAX_NUMBERED_KEYS = 100


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
        add(name)
    return ordered


class KeyPool:
    """Retry the same logical request on the next key when quota is exhausted."""

    def __init__(self, api_key_env_vars: tuple[str, ...]):
        self._configured = api_key_env_vars

    def available_keys(self, environ: Mapping[str, str] | None = None) -> list[str]:
        return discover_key_env_vars(self._configured, environ)

    def api_key_for(self, key_var: str, environ: Mapping[str, str] | None = None) -> str:
        env = os.environ if environ is None else environ
        value = (env.get(key_var) or "").strip()
        if not value:
            raise MissingApiKeyError(f"Environment variable {key_var} is not set.")
        return value

    def call(self, fn: Callable[[str], T], *, environ: Mapping[str, str] | None = None) -> T:
        keys = self.available_keys(environ)
        if not keys:
            raise MissingApiKeyError(
                "No API keys found. Set one of: " + ", ".join(self._configured)
            )
        last_exc: QuotaExhaustedError | None = None
        for idx, key_var in enumerate(keys):
            try:
                return fn(key_var)
            except QuotaExhaustedError as exc:
                last_exc = exc
                if idx == len(keys) - 1:
                    break
        raise QuotaExhaustedError(
            f"All {len(keys)} API key(s) are exhausted ({', '.join(keys)})."
        ) from last_exc
