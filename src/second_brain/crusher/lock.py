"""Single-instance lock for the crusher."""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from ..io_utils import atomic_write_text

logger = logging.getLogger(__name__)


class CrusherLockError(RuntimeError):
    """Another crusher run holds the lock."""


class CrusherLock:
    def __init__(self, path: Path, *, stale_hours: float):
        self.path = Path(path)
        self.stale_hours = stale_hours
        self._held = False

    def __enter__(self) -> CrusherLock:
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()

    def acquire(self) -> None:
        if self.path.is_file():
            try:
                mtime = datetime.fromtimestamp(self.path.stat().st_mtime, tz=timezone.utc)
                age_hours = (datetime.now(timezone.utc) - mtime).total_seconds() / 3600.0
            except OSError:
                age_hours = 0.0
            if age_hours < self.stale_hours:
                raise CrusherLockError(
                    f"Another crusher run may be active (lock: {self.path}). "
                    f"Delete the lock if the prior run crashed."
                )
            logger.warning("Removing stale crusher lock (%.1fh old).", age_hours)
            self.path.unlink(missing_ok=True)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(self.path, f"pid={os.getpid()}\n")
        self._held = True

    def release(self) -> None:
        if self._held:
            self.path.unlink(missing_ok=True)
            self._held = False
