"""Crusher progress and analysis cache (resumable, cache-first)."""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ..io_utils import atomic_write_json, atomic_write_text
from ..urls import normalize_url
from .schema import CrusherAnalysis

logger = logging.getLogger(__name__)

STATUS_PENDING = "pending"
STATUS_DONE = "done"
STATUS_UNAVAILABLE = "unavailable"
STATUS_NEEDS_REVIEW = "needs-review"
STATUS_FAILED = "failed"
# 429 / timeout / network: retried on the next run (unlike "unavailable").
STATUS_FAILED_RETRYABLE = "failed-retryable"
# Every tier and retry failed. Listed in crusher_manual_review.md; not retried
# until --reprocess. Kept separate from needs-review (a usable but incomplete analysis).
STATUS_MANUAL_REVIEW = "manual-review"


@dataclass
class UrlState:
    url: str
    status: str
    prompt_version: str
    attempts: int = 0
    last_error: str | None = None
    updated_at: str | None = None


class CrusherState:
    """Append-only state log plus per-URL latest view and on-disk analysis cache."""

    def __init__(self, state_path: Path, cache_dir: Path):
        self.state_path = Path(state_path)
        self.cache_dir = Path(cache_dir)
        self._latest: dict[str, UrlState] = {}
        self._load()

    @staticmethod
    def url_hash(url: str) -> str:
        return hashlib.sha256(normalize_url(url).encode("utf-8")).hexdigest()[:16]

    def cache_path(self, url: str) -> Path:
        return self.cache_dir / f"{self.url_hash(url)}.json"

    def _load(self) -> None:
        if not self.state_path.is_file():
            return
        with open(self.state_path, "r", encoding="utf-8") as handle:
            for line_no, line in enumerate(handle, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                    url = normalize_url(record["url"])
                    self._latest[url] = UrlState(
                        url=url,
                        status=str(record.get("status", STATUS_PENDING)),
                        prompt_version=str(record.get("prompt_version", "")),
                        attempts=int(record.get("attempts", 0)),
                        last_error=record.get("last_error"),
                        updated_at=record.get("updated_at"),
                    )
                except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                    logger.warning("Ignoring malformed crusher state line %s:%d (%s)", self.state_path, line_no, exc)

    def get(self, url: str) -> UrlState | None:
        return self._latest.get(normalize_url(url))

    def record(
        self,
        url: str,
        *,
        status: str,
        prompt_version: str,
        attempts: int | None = None,
        last_error: str | None = None,
    ) -> None:
        url = normalize_url(url)
        prev = self._latest.get(url)
        attempt_count = attempts if attempts is not None else (prev.attempts if prev else 0)
        state = UrlState(
            url=url,
            status=status,
            prompt_version=prompt_version,
            attempts=attempt_count,
            last_error=last_error,
            updated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
        self._latest[url] = state
        record = {
            "url": url,
            "status": status,
            "prompt_version": prompt_version,
            "attempts": attempt_count,
            "last_error": last_error,
            "updated_at": state.updated_at,
        }
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.state_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def save_analysis(self, url: str, analysis: CrusherAnalysis, *, prompt_version: str, passes: int) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "url": normalize_url(url),
            "prompt_version": prompt_version,
            "passes": passes,
            "analysis": analysis.model_dump(),
        }
        atomic_write_json(self.cache_path(url), payload)

    def load_analysis(self, url: str, *, prompt_version: str) -> CrusherAnalysis | None:
        path = self.cache_path(url)
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Could not read crusher cache %s: %s", path.name, exc)
            return None
        if str(payload.get("prompt_version")) != str(prompt_version):
            return None
        try:
            return CrusherAnalysis.model_validate(payload["analysis"])
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning("Invalid crusher cache for %s: %s", url, exc)
            return None

    # -- stage caches -------------------------------------------------------
    # crusher_cache/<hash>/<stage>.json holds prompt-independent work (yt-dlp
    # metadata, transcripts). A prompt_version bump re-runs only the paid
    # summary/classification, never the download or transcription.

    def stage_path(self, url: str, stage: str) -> Path:
        safe = "".join(ch for ch in stage if ch.isalnum() or ch in "-_") or "stage"
        return self.cache_dir / self.url_hash(url) / f"{safe}.json"

    def load_stage(self, url: str, stage: str) -> dict | None:
        path = self.stage_path(url, stage)
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Ignoring unreadable stage cache %s: %s", path, exc)
            return None
        return payload if isinstance(payload, dict) else None

    def save_stage(self, url: str, stage: str, payload: dict) -> None:
        path = self.stage_path(url, stage)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(path, payload)

    def clear_stages(self, url: str) -> None:
        """Used by --reprocess so stale caption URLs / transcripts are refetched."""
        folder = self.cache_dir / self.url_hash(url)
        if not folder.is_dir():
            return
        for path in folder.glob("*.json"):
            try:
                path.unlink()
            except OSError as exc:
                logger.warning("Could not delete stage cache %s: %s", path, exc)

    def attempts(self, url: str) -> int:
        state = self.get(url)
        return state.attempts if state else 0

    def should_skip(self, url: str, *, prompt_version: str, reprocess: bool) -> bool:
        if reprocess:
            return False
        state = self.get(url)
        if not state or state.prompt_version != prompt_version:
            return False
        # needs-review and manual-review are finished enough to apply from cache.
        # --reprocess is how a later run tries them again.
        return state.status in {STATUS_DONE, STATUS_UNAVAILABLE, STATUS_NEEDS_REVIEW, STATUS_MANUAL_REVIEW}
