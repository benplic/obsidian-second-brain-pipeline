"""Rewrite legacy card frontmatter onto the shared contract.

Dry-run by default. ``--apply`` writes. Human triage statuses
(``kept``, ``promoted``, ``tossed``) are not changed. Pipeline words that
used to live in ``status`` (``ready``, ``To Watch``, ``manual-review``,
``archived``) move to ``stage``, and ``status`` becomes ``inbox``.
"""

from __future__ import annotations

import logging
from pathlib import Path

from .config import Settings
from .io_utils import atomic_write_text
from .model_c.hubs import PREVIOUS_HUBS, hub_for
from .vault import parse_frontmatter_strict, rebuild_card

logger = logging.getLogger(__name__)

# Pipeline states that were stored in ``status`` before ``stage`` existed.
LEGACY_STATUS_TO_STAGE = {
    "to watch": "to-watch",
    "manual-review": "manual-review",
    "ready": "ready",
    "archived": "archived",
}

_SKIP_SUFFIXES = ("Hub.md", "Kanban.md", "Map.md")


def rewrite_tag(tag: str) -> str:
    """Map the old tag spellings onto the current ones."""
    if tag == "second-brain":
        return "saved-media"
    if not tag.startswith("category/"):
        return tag
    rest = tag[len("category/"):]
    slug = "-".join(part for part in rest.replace("&", " and ").replace("/", " ").replace("-", " ").split() if part)
    return f"category/{slug}" if slug else tag


def migrate_frontmatter(fm: dict) -> bool:
    """Update ``fm`` in place. Return True when something changed."""
    changed = False
    status = str(fm.get("status") or "")
    stage = LEGACY_STATUS_TO_STAGE.get(status.lower())
    if stage is not None:
        if fm.get("stage") != stage:
            fm["stage"] = stage
            changed = True
        if status != "inbox":
            fm["status"] = "inbox"
            changed = True
    if fm.get("category") == "Music":
        fm["category"] = "Saved Music"
        changed = True
    tags = fm.get("tags")
    if isinstance(tags, list):
        rewritten: list = []
        for tag in tags:
            if not isinstance(tag, str):
                rewritten.append(tag)
                continue
            new = rewrite_tag(tag)
            if new != tag:
                changed = True
            if new not in rewritten:
                rewritten.append(new)
            elif new != tag:
                changed = True
        if rewritten != tags:
            fm["tags"] = rewritten
            changed = True
    return changed


def _refresh_hub(path: Path, apply: bool) -> bool:
    try:
        current = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        logger.warning("Skipping hub %s: %s", path, exc)
        return False
    previous = PREVIOUS_HUBS.get(path.name)
    if previous is None or current != previous:
        return False
    _, body = hub_for(path.parent.name)
    logger.info("%s stock hub %s", "Updating" if apply else "Would update", path)
    if apply:
        atomic_write_text(path, body)
    return True


def run_migrate_cards(settings: Settings, *, apply: bool) -> int:
    """Return how many notes would change, or did change when ``apply`` is set."""
    settings.require_vault()
    root = settings.resources_dir
    if not root.is_dir():
        logger.info("No resources folder at %s", root)
        return 0
    changes = 0
    for path in sorted(root.rglob("*.md")):
        if any(path.name.endswith(suffix) for suffix in _SKIP_SUFFIXES):
            if _refresh_hub(path, apply):
                changes += 1
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            logger.warning("Skipping %s: %s", path, exc)
            continue
        fm = parse_frontmatter_strict(content)
        if not isinstance(fm, dict) or not fm:
            continue
        updated = dict(fm)
        if not migrate_frontmatter(updated):
            continue
        changes += 1
        logger.info("%s %s", "Updating" if apply else "Would update", path)
        if apply:
            atomic_write_text(path, rebuild_card(content, updated))
    logger.info("%s %d note(s).", "Updated" if apply else "Would update", changes)
    return changes
