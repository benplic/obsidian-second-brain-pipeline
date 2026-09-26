"""Durable file helpers shared by every pipeline step.

The pipeline treats a few local files as destructive queues. Losing or
corrupting one of them means either re-spending scarce Gemini requests or
silently dropping saved videos, so every rewrite goes through
write-temp -> flush -> fsync -> os.replace. A crash therefore leaves either the
old file or the new file on disk, never a half-written one.
"""

from __future__ import annotations

import csv
import json
import logging
import os
import uuid
from pathlib import Path
from typing import Any, Iterable, Sequence

logger = logging.getLogger(__name__)


def _fsync_directory(directory: Path) -> None:
    """Best-effort fsync of a directory so a rename survives power loss.

    Windows cannot open directories as files, so this is POSIX-only. Failing
    here is not fatal: the file contents are already synced.
    """
    if os.name == "nt":
        return
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError as exc:
        logger.debug("Could not open %s for fsync: %s", directory, exc)
        return
    try:
        os.fsync(fd)
    except OSError as exc:
        logger.debug("Directory fsync failed for %s: %s", directory, exc)
    finally:
        os.close(fd)


def atomic_write_text(path: Path, content: str, *, encoding: str = "utf-8", newline: str | None = None) -> None:
    """Write ``content`` to ``path`` atomically (temp file + fsync + replace)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Dot-prefixed temp name keeps Obsidian from indexing a half-written card.
    tmp_path = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        with open(tmp_path, "w", encoding=encoding, newline=newline) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        # Includes KeyboardInterrupt: never leave temp litter behind in the vault.
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError as cleanup_exc:
            logger.warning("Could not remove temp file %s: %s", tmp_path, cleanup_exc)
        raise
    _fsync_directory(path.parent)


def atomic_write_json(path: Path, data: Any) -> None:
    """Serialize ``data`` as pretty JSON and write it atomically."""
    atomic_write_text(path, json.dumps(data, indent=2, ensure_ascii=False))


def read_json_list(path: Path) -> list[dict]:
    """Read a JSON array queue file. Missing file -> empty list.

    Raises:
        QueueCorruptError: the file exists but is not a JSON array. The original
            scripts silently overwrote a corrupt queue, which destroyed it; we
            stop instead so the user can inspect it.
    """
    path = Path(path)
    if not path.exists():
        return []
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except json.JSONDecodeError as exc:
        raise QueueCorruptError(f"{path} is not valid JSON ({exc}). Fix or move it aside, then re-run.") from exc
    if not isinstance(data, list):
        raise QueueCorruptError(f"{path} must contain a JSON array, found {type(data).__name__}.")
    return data


def read_csv_rows(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    """Read a CSV queue (utf-8-sig so Excel-edited files still parse)."""
    path = Path(path)
    if not path.exists():
        return [], []
    with open(path, "r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
        return list(reader.fieldnames or []), rows


def atomic_write_csv(path: Path, fieldnames: Sequence[str], rows: Iterable[dict]) -> None:
    """Rewrite a CSV queue atomically, preserving the utf-8-sig BOM for Excel."""
    import io

    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(fieldnames), extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    atomic_write_text(path, buffer.getvalue(), encoding="utf-8-sig", newline="")


def append_csv_rows(path: Path, fieldnames: Sequence[str], rows: Iterable[dict]) -> None:
    """Append rows to a CSV queue and fsync before returning.

    Append (not rewrite) matches the original Step 2 behavior; the header is
    written only when the file is new.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    file_exists = path.exists() and path.stat().st_size > 0
    with open(path, "a", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fieldnames), extrasaction="ignore")
        if not file_exists:
            writer.writeheader()
        writer.writerows(rows)
        handle.flush()
        os.fsync(handle.fileno())


def remove_if_exists(path: Path) -> bool:
    """Delete an emptied queue file. Returns True when something was deleted."""
    try:
        Path(path).unlink()
        return True
    except FileNotFoundError:
        return False


class QueueCorruptError(RuntimeError):
    """A queue file exists but cannot be parsed; refuse to overwrite it."""
