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


def parse_frontmatter_strict(content: str) -> dict | None:
    """Parse frontmatter without the regex fallback.

    Returns ``{}`` when the note has no frontmatter and ``None`` when the YAML
    block exists but does not parse to a mapping. Anything that rewrites the
    whole frontmatter must use this: rewriting from the lenient regex result
    would silently drop every key except ``url``/``status``.
    """
    fm_text, _ = split_frontmatter(content)
    if fm_text is None:
        return {}
    try:
        data = yaml.safe_load(fm_text)
    except yaml.YAMLError as exc:
        logger.debug("Strict frontmatter parse failed: %s", exc)
        return None
    if data is None:
        return {}
    return data if isinstance(data, dict) else None


# Stable frontmatter key order for rewritten cards. Keys not listed keep their
# existing relative order after these.
DEFAULT_KEY_ORDER: tuple[str, ...] = (
    "category",
    "creator",
    "url",
    "status",
    "tags",
    "summary",
    "location",
    "location_name",
    "weight",
    "mapMarkerColor",
)


def _as_int(value: object, default: int = 0) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _yaml_scalar(value: object) -> str:
    """Render one scalar the way the pipeline writes it (numbers bare, text quoted)."""
    if value is None:
        return '""'
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, dict):
        # JSON is valid YAML flow syntax; nested maps are rare in cards.
        return json.dumps(value, ensure_ascii=False, default=str)
    return yaml_str(value)


def format_frontmatter(fm: dict, key_order: tuple[str, ...] = DEFAULT_KEY_ORDER) -> str:
    """Serialize frontmatter in pipeline order with YAML-safe scalars.

    ``tags`` stay unquoted block items (what every existing card uses),
    ``location`` pairs stay inline ``[lat, lng]`` for Map View, and other lists
    become quoted block items.
    """
    lines: list[str] = []

    def emit(key: str, value: object) -> None:
        if key == "tags":
            if not isinstance(value, list):
                lines.append(f"tags: {yaml_str(value)}")
            elif not value:
                lines.append("tags: []")
            else:
                lines.append("tags:")
                lines.extend(f"  - {tag}" for tag in value)
            return
        if key == "weight":
            lines.append(f"weight: {_as_int(value)}")
            return
        if key == "location" and isinstance(value, (list, tuple)) and len(value) == 2:
            lines.append(f"location: [{value[0]}, {value[1]}]")
            return
        if key == "url":
            lines.append(f"url: {yaml_str(value)}")
            return
        if isinstance(value, (list, tuple)):
            if not value:
                lines.append(f"{key}: []")
            else:
                lines.append(f"{key}:")
                lines.extend(f"  - {_yaml_scalar(item)}" for item in value)
            return
        lines.append(f"{key}: {_yaml_scalar(value)}")

    seen: set[str] = set()
    for key in key_order:
        if key in fm:
            emit(key, fm[key])
            seen.add(key)
    for key, value in fm.items():
        if key not in seen:
            emit(str(key), value)
    return "\n".join(lines)


def rebuild_card(content: str, new_fm: dict) -> str:
    """Replace the YAML block; preserve the note body byte-for-byte."""
    fm_text, body = split_frontmatter(content)
    if fm_text is None:
        raise ValueError("Card has no YAML frontmatter block")
    # Body from split_frontmatter is everything after the closing ---;
    # keep leading newlines as stored so diffs stay small.
    if body.startswith("\n") or body == "":
        return f"---\n{format_frontmatter(new_fm)}\n---\n{body}"
    return f"---\n{format_frontmatter(new_fm)}\n---\n\n{body}"


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
