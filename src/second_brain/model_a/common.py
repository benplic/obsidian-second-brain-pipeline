"""Shared Model A routing rules."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

CategoryType = Literal["Tech & Coding", "Project Ideas", "Movies & Shows", "Other / Ambiguous"]
MODEL_A_CATEGORIES = ["Tech & Coding", "Project Ideas", "Movies & Shows", "Other / Ambiguous"]
MANUAL_REVIEW_SUBPATH = Path("Inbox") / "Manual Review"
KANBAN_FILENAME = "!Watchlist Kanban.md"

# Statuses a human sets during triage. Model A must not reset them to "inbox".
HUMAN_STATUSES = {"kept", "promoted", "tossed"}


@dataclass(frozen=True)
class Route:
    target_dir: Path
    stage: str | None
    category_label: str


def route(resources_dir: Path, category: str, confidence: float, needs_manual_review: bool, threshold: float,
          *, is_movie_with_title: bool = True) -> Route:
    """Folder and stage for a category that was already chosen.

    ``status`` is not decided here. New cards stay ``inbox``; movies record
    ``stage: to-watch`` so the Kanban column is not a triage status.
    """
    if needs_manual_review or confidence < threshold or category == "Other / Ambiguous":
        return Route(resources_dir / MANUAL_REVIEW_SUBPATH, "manual-review", "Miscellaneous")
    if category == "Movies & Shows" and is_movie_with_title:
        return Route(resources_dir / "Movies & Shows", "to-watch", "Movies & Shows")
    return Route(resources_dir / category, None, category)
