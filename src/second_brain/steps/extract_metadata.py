"""Step 1: pending_links.txt -> clean_metadata.json via multi-threaded yt-dlp.

Zero waste: URLs already known (vault, ledger, in flight) are dropped before
any extraction. On exit, pending_links.txt keeps only failed/invalid links (for
review) plus anything the phone appended while we were running, and is deleted
when nothing is left.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ..config import Settings
from ..io_utils import atomic_write_json, atomic_write_text, read_json_list, remove_if_exists
from ..ledger import UrlLedger
from ..urls import is_valid_http_url, normalize_url
from .registry import known_urls

logger = logging.getLogger(__name__)

# Suppress console window popups for each yt-dlp call on Windows.
_CREATION_FLAGS = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

Fetcher = Callable[[str], "dict | None"]


@dataclass
class ExtractResult:
    extracted: int = 0
    skipped_known: int = 0
    failed: list[str] = field(default_factory=list)
    invalid: list[str] = field(default_factory=list)


def build_ytdlp_command(url: str, cookies_from_browser: str | None = None) -> list[str]:
    cmd = ["yt-dlp", "--dump-json", "--skip-download", "--ignore-errors"]
    if cookies_from_browser:
        cmd += ["--cookies-from-browser", cookies_from_browser]
    # "--" ends option parsing so a URL can never be read as a yt-dlp flag.
    return cmd + ["--", url]


def fetch_metadata(url: str, timeout_seconds: int = 20, cookies_from_browser: str | None = None) -> dict | None:
    """Metadata for one URL via yt-dlp, or None if it could not be fetched."""
    try:
        result = subprocess.run(
            build_ytdlp_command(url, cookies_from_browser),
            capture_output=True,
            text=True,
            encoding="utf-8",
            creationflags=_CREATION_FLAGS,
            timeout=timeout_seconds,  # dead links can hang yt-dlp indefinitely
        )
    except subprocess.TimeoutExpired:
        logger.warning("yt-dlp timed out after %ss: %s", timeout_seconds, url)
        return None
    except FileNotFoundError:
        # Not per-URL: yt-dlp is missing entirely. Fail loudly instead of
        # marking every link as "failed".
        raise
    except OSError as exc:
        logger.warning("yt-dlp could not start for %s: %s", url, exc)
        return None

    if result.returncode != 0 or not result.stdout.strip():
        logger.debug("yt-dlp failed (rc=%s) for %s: %s", result.returncode, url, (result.stderr or "").strip()[:300])
        return None
    try:
        # --dump-json prints one JSON object per line; a single video URL gives one.
        info = json.loads(result.stdout.strip().splitlines()[0])
    except json.JSONDecodeError as exc:
        logger.warning("yt-dlp returned invalid JSON for %s: %s", url, exc)
        return None
    return {
        "url": url,
        "title": info.get("title") or "",
        "description": info.get("description") or "",
        "tags": info.get("tags") or [],
        "creator": info.get("uploader") or "Unknown",
    }


def _read_lines(path: Path) -> list[str]:
    with open(path, "r", encoding="utf-8") as handle:
        return [line.strip() for line in handle if line.strip()]


def _rewrite_pending(path: Path, initial_lines: set[str], keep: list[str]) -> None:
    """Rewrite pending_links.txt with ``keep`` plus any lines added since we read it.

    The iOS Shortcut appends through a cloud-drive sync that can land at any
    moment; the original rewrote/deleted the file blindly and could lose a link
    captured mid-run.

    TODO: A sync client can still write between this re-read and the replace.
    Closing that fully needs the Shortcut to write one file per link.
    """
    appended = [line for line in _read_lines(path) if line not in initial_lines] if path.exists() else []
    if appended:
        logger.info("%d link(s) were added while extracting; kept for the next run.", len(appended))
    final = list(dict.fromkeys(keep + appended))
    if final:
        atomic_write_text(path, "".join(f"{line}\n" for line in final))
    elif remove_if_exists(path):
        logger.info("CLEANUP: All links processed. '%s' deleted.", path.name)


def run_extract(settings: Settings, fetcher: Fetcher | None = None) -> ExtractResult:
    result = ExtractResult()
    pending_path = settings.pending_links_path
    if not pending_path.exists():
        logger.info("Queue empty: '%s' not found. Skipping extraction.", pending_path)
        return result

    raw_lines = _read_lines(pending_path)
    initial_lines = set(raw_lines)
    if not raw_lines:
        _rewrite_pending(pending_path, initial_lines, [])
        return result

    candidates: list[str] = []
    for line in raw_lines:
        if is_valid_http_url(line):
            candidates.append(normalize_url(line))
        else:
            result.invalid.append(line)
    if result.invalid:
        logger.warning("Kept %d line(s) that are not http(s) URLs in '%s' for review.", len(result.invalid), pending_path.name)
    candidates = list(dict.fromkeys(candidates))

    # The ledger lives in the vault by default; never create a vault by accident.
    settings.require_vault()
    ledger = UrlLedger(settings.ledger_path)
    known = known_urls(settings, ledger)
    to_process = [url for url in candidates if url not in known]
    result.skipped_known = len(candidates) - len(to_process)
    if result.skipped_known:
        logger.info("Skipped %d URL(s) already in The Brain, the ledger, or the queue.", result.skipped_known)

    if not to_process:
        _rewrite_pending(pending_path, initial_lines, result.invalid)
        return result

    if fetcher is None:
        cfg = settings.extract

        def fetcher(url: str) -> dict | None:
            return fetch_metadata(url, cfg.timeout_seconds, cfg.cookies_from_browser)

    # Load the existing queue up front: a corrupt file raises QueueCorruptError
    # here, before any work is done, instead of being overwritten at the end.
    queue = read_json_list(settings.clean_metadata_path)
    new_items: list[dict] = []
    logger.info("Extracting metadata for %d new link(s) (%d workers)...", len(to_process), settings.extract.workers)

    with ThreadPoolExecutor(max_workers=settings.extract.workers) as executor:
        futures = {executor.submit(fetcher, url): url for url in to_process}
        for count, future in enumerate(as_completed(futures), 1):
            url = futures[future]
            try:
                item = future.result()
            except FileNotFoundError as exc:
                raise RuntimeError("yt-dlp not found on PATH. Install it with: pip install yt-dlp") from exc
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                logger.warning("Extraction error for %s: %s", url, exc)
                item = None
            if item:
                new_items.append(item)
            else:
                result.failed.append(url)
            logger.info("  Processed %d/%d", count, len(to_process))
            # Checkpoint so a crash mid-run keeps finished work. A re-run then
            # sees those URLs as in flight and skips them.
            if new_items and count % settings.extract.checkpoint_every == 0:
                atomic_write_json(settings.clean_metadata_path, queue + new_items)

    if new_items:
        atomic_write_json(settings.clean_metadata_path, queue + new_items)
        logger.info("Saved metadata for %d video(s) to '%s'.", len(new_items), settings.clean_metadata_path.name)
    result.extracted = len(new_items)

    if result.failed:
        logger.warning("CLEANUP: Kept %d failed/dead link(s) in '%s' for review.", len(result.failed), pending_path.name)
    _rewrite_pending(pending_path, initial_lines, result.failed + result.invalid)
    return result
