"""Step 1 (alternate): Meta ``saved_posts.json`` export -> clean_metadata.json.

Historical Instagram saves skip yt-dlp entirely. The export is personal data
and is gitignored; it is read in place and never modified.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from ..config import Settings
from ..io_utils import atomic_write_json, read_json_list
from ..ledger import UrlLedger
from ..urls import normalize_url
from .registry import known_urls

logger = logging.getLogger(__name__)

_HASHTAG_RE = re.compile(r"#(\w+)")


class InstagramExportError(ValueError):
    """The export file is missing or not in the expected shape."""


@dataclass
class InstagramResult:
    parsed: int = 0
    queued: int = 0
    skipped_known: int = 0


def extract_tags(text: str) -> list[str]:
    """Hashtags from a caption, de-duplicated, first-seen order.

    (The original used ``list(set(...))``, whose order changed between runs.)
    """
    return list(dict.fromkeys(_HASHTAG_RE.findall(text or "")))


def parse_saved_posts(data: object) -> list[dict]:
    """Turn the Meta export structure into pipeline metadata items."""
    # Exports sometimes wrap the array in a top-level key.
    posts = data.get("saved_saved_media", data) if isinstance(data, dict) else data
    if not isinstance(posts, list):
        raise InstagramExportError("Expected a list of saved posts (or a 'saved_saved_media' key).")

    items: list[dict] = []
    for post in posts:
        if not isinstance(post, dict):
            logger.debug("Skipping non-object entry in export: %r", post)
            continue
        url, caption, creator = "", "", "Unknown"
        # All useful data sits inside 'label_values'.
        for entry in post.get("label_values", []):
            label = entry.get("label", "")
            if label == "URL":
                url = entry.get("href", entry.get("value", ""))
            elif label == "Caption":
                caption = entry.get("value", "")
            elif entry.get("title", "") == "Owner":
                owner_dicts = entry.get("dict", [])
                if owner_dicts:
                    for attr in owner_dicts[0].get("dict", []):
                        if attr.get("label") == "Username":
                            creator = attr.get("value", creator)
        if not url:
            continue
        # Instagram has no titles; use the first 60 caption characters.
        title = caption[:60] + "..." if len(caption) > 60 else caption
        items.append({
            "url": url,
            "title": title.strip() or "Instagram Saved Post",
            "description": caption,
            "tags": extract_tags(caption),
            "creator": creator,
        })
    return items


def run_parse_instagram(settings: Settings, export_path: Path | None = None) -> InstagramResult:
    path = Path(export_path or settings.instagram_export_path)
    if not path.is_file():
        raise InstagramExportError(
            f"Instagram export not found: {path}. Set instagram_export_path in config.yaml."
        )
    logger.info("Reading %s...", path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise InstagramExportError(f"{path} is not valid JSON: {exc}") from exc

    items = parse_saved_posts(data)
    result = InstagramResult(parsed=len(items))

    # Behavior change vs original: dedupe against the vault/ledger/queue and
    # append to the existing queue. The original overwrote clean_metadata.json,
    # destroying any pending Step 2 work, and re-sent saved videos to Gemini.
    settings.require_vault()
    ledger = UrlLedger(settings.ledger_path)
    known = known_urls(settings, ledger)
    queue = read_json_list(settings.clean_metadata_path)
    new_items: list[dict] = []
    for item in items:
        url = normalize_url(item["url"])
        if url in known:
            result.skipped_known += 1
            continue
        known.add(url)
        new_items.append({**item, "url": url})

    if new_items:
        atomic_write_json(settings.clean_metadata_path, queue + new_items)
    result.queued = len(new_items)
    logger.info(
        "Parsed %d saved post(s): queued %d, skipped %d already known. Next: run 'categorize'.",
        result.parsed, result.queued, result.skipped_known,
    )
    return result
