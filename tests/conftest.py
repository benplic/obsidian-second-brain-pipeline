"""Shared fixtures: a throwaway vault + data dir, and a fake Gemini client.

No test touches the network or a real vault.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from google.genai import errors as genai_errors

from second_brain.config import settings_from_dict


@pytest.fixture
def settings(tmp_path: Path):
    vault = tmp_path / "vault"
    (vault / "3 - Resources").mkdir(parents=True)
    return settings_from_dict(
        {
            "vault_path": str(vault),
            "data_dir": str(tmp_path / "data"),
            "gemini": {"pause_between_batches_seconds": 0, "backoff_base_seconds": 8},
        },
        base_dir=tmp_path,
    )


def make_card(resources: Path, folder: str, name: str, url: str, status: str = "inbox", quoted: bool = True) -> Path:
    path = resources / folder / f"{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    url_line = f'url: "{url}"' if quoted else f"url: {url}"
    path.write_text(f'---\ncategory: "X"\n{url_line}\nstatus: "{status}"\n---\n\n# {name}\n', encoding="utf-8")
    return path


def rate_limit_error(per_day: bool = False) -> genai_errors.ClientError:
    quota = "GenerateRequestsPerDayPerProjectPerModel-FreeTier" if per_day else "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"
    return genai_errors.ClientError(429, {"error": {"code": 429, "message": f"Quota exceeded. quotaId: {quota}", "status": "RESOURCE_EXHAUSTED"}})


class FakeModels:
    """Stands in for ``client.models``. Each queued response is either a JSON
    string (returned as ``.text``) or an exception to raise."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        if not self.responses:
            raise AssertionError("Unexpected extra Gemini call")
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return SimpleNamespace(text=item)


class FakeClient:
    def __init__(self, responses):
        self.models = FakeModels(responses)


class FakeRuntime:
    """Stands in for ``LlmRuntime`` in offline pipeline tests."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list = []
        self.adapter = SimpleNamespace(provider_label="fake")

    def complete(self, request, *, sleep=None, rng=None):
        import random
        import time

        from second_brain.gemini import call_with_backoff
        from second_brain.llm.types import CompletionResult, QuotaExhaustedError, RateLimitError

        sleep = sleep or time.sleep
        rng = rng or random.uniform

        def once():
            if not self._responses:
                raise AssertionError("Unexpected extra LLM call")
            item = self._responses.pop(0)
            self.calls.append(request)
            if isinstance(item, BaseException):
                if isinstance(item, genai_errors.ClientError) and item.code == 429:
                    if "PerDay" in str(item):
                        raise QuotaExhaustedError(str(item))
                    raise RateLimitError(str(item))
                raise item
            return CompletionResult(text=item)

        return call_with_backoff(
            once,
            max_retries=5,
            base_seconds=8,
            jitter=(1.0, 3.0),
            sleep=sleep,
            rng=rng,
        )


def mega_response(n: int, cat: str = "Tech & Coding", skip: set[int] | None = None) -> str:
    skip = skip or set()
    return json.dumps({"results": [{"i": i, "cat": cat, "summ": f"summary {i}"} for i in range(n) if i not in skip]})


def videos(n: int, start: int = 0) -> list[dict]:
    return [
        {"url": f"https://www.tiktok.com/@u/video/{i}", "title": f"Video {i}", "description": f"desc {i}",
         "tags": ["a", "b"], "creator": f"creator{i}"}
        for i in range(start, start + n)
    ]
