"""Obsidian vault access: frontmatter parsing, URL registry scan, card writing.

The vault is the master registry: a URL that appears in any card's
frontmatter under ``3 - Resources/`` is considered already saved.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from .io_utils import atomic_write_text
from .urls import normalize_url

logger = logging.getLogger(__name__)

_FRONTMATTER_RE = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.DOTALL)
# Fallback when a hand-edited card has YAML that no longer parses. Accepts the
# quoted form the pipeline writes and the unquoted form people type by hand.
_URL_LINE_RE = re.compile(r"^url:\s*[\"']?([^\"'\r\n]+)[\"']?\s*$", re.MULTILINE)
_STATUS_LINE_RE = re.compile(r"^status:\s*[\"']?([^\"'\r\n]+)[\"']?\s*$", re.MULTILINE)

_ILLEGAL_FILENAME_CHARS = re.compile(r'[\\/*?:"<>|]')
_WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


@dataclass(frozen=True)
class CardRef:
    """A card found in the vault that carries a URL."""

    url: str
    status: str
    path: Path


def split_frontmatter(content: str) -> tuple[str | None, str]:
    """Return (frontmatter_text, body). frontmatter_text is None when absent."""
    match = _FRONTMATTER_RE.match(content)
    if not match:
        return None, content
    return match.group(1), content[match.end():]


def parse_frontmatter(content: str) -> dict:
    """Parse card frontmatter into a dict, tolerating broken YAML.

    Only the frontmatter block is inspected (the original regex scanned the
    whole file, so a ``url:`` line inside a note body could count as saved).
    """
    fm_text, _ = split_frontmatter(content)
    if fm_text is None:
        return {}
    try:
        data = yaml.safe_load(fm_text)
        if isinstance(data, dict):
            return data
        logger.debug("Frontmatter is not a mapping; falling back to regex")
    except yaml.YAMLError as exc:
        logger.debug("Frontmatter YAML error, falling back to regex: %s", exc)
    result: dict = {}
    if url_match := _URL_LINE_RE.search(fm_text):
        result["url"] = url_match.group(1).strip()
    if status_match := _STATUS_LINE_RE.search(fm_text):
        result["status"] = status_match.group(1).strip()
    return result


def iter_cards(resources_dir: Path):
    """Yield CardRef for every Markdown file with a ``url`` in its frontmatter."""
    resources_dir = Path(resources_dir)
    if not resources_dir.is_dir():
        logger.warning("Resources folder not found, treating vault as empty: %s", resources_dir)
        return
    for root, dirs, files in os.walk(resources_dir):
        # Skip dot-folders (.obsidian, .trash) so trashed cards are not "saved".
        # Trashed/deleted cards are covered by the ledger instead.
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for name in files:
            if not name.endswith(".md") or name.startswith("."):
                continue
            path = Path(root) / name
            try:
                content = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                # One unreadable note must not stop the whole scan, but the
                # original swallowed this silently; surface it.
                logger.warning("Skipping unreadable note %s: %s", path, exc)
                continue
            fm = parse_frontmatter(content)
            url = fm.get("url")
            if isinstance(url, str) and url.strip():
                yield CardRef(url=normalize_url(url), status=str(fm.get("status") or ""), path=path)


def get_vault_urls(resources_dir: Path) -> set[str]:
    """Set of normalized URLs already saved as cards."""
    return {card.url for card in iter_cards(resources_dir)}


def sanitize_filename(title: str, max_len: int = 60, fallback: str = "Saved Video") -> str:
    """Make a Windows-safe file stem from a video title."""
    cleaned = _ILLEGAL_FILENAME_CHARS.sub("", title or "")
    cleaned = " ".join(cleaned.split())[:max_len].strip()
    # Windows silently drops trailing dots/spaces, which would break our
    # exists() checks, and refuses reserved device names.
    cleaned = cleaned.rstrip(". ")
    if not cleaned:
        return fallback
    if cleaned.split(".")[0].upper() in _WINDOWS_RESERVED:
        cleaned = f"_{cleaned}"
    return cleaned


def unique_card_path(target_dir: Path, stem: str) -> Path:
    """First free path among ``stem.md``, ``stem (2).md``, ``stem (3).md`` ...

    The original Step 3 named files ``<title> (<csv row>).md`` and treated an
    existing file as "already done". Row numbers restart every run, so a new
    video with the same title and row as an old card was silently dropped.
    Dedupe is now by URL; file names only need to be unique.
    """
    candidate = target_dir / f"{stem}.md"
    counter = 2
    while candidate.exists():
        candidate = target_dir / f"{stem} ({counter}).md"
        counter += 1
    return candidate


def yaml_str(value: object) -> str:
    """Double-quoted YAML scalar. JSON strings are valid YAML, and this escapes
    quotes/backslashes/newlines that the original f-strings let break the card."""
    return json.dumps("" if value is None else str(value), ensure_ascii=False)


def write_card(target_dir: Path, stem: str, content: str) -> Path:
    """Atomically write a new card (temp + fsync + replace) without overwriting.

    Single-writer assumption: the pipeline is the only process creating cards,
    so the gap between choosing a free name and the rename is acceptable.
    """
    target_dir.mkdir(parents=True, exist_ok=True)
    path = unique_card_path(target_dir, stem)
    atomic_write_text(path, content)
    return path
