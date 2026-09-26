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
    status: str
    category_label: str


def route(resources_dir: Path, category: str, confidence: float, needs_manual_review: bool, threshold: float,
          *, is_movie_with_title: bool = True) -> Route:
    """Decide folder/status. Low confidence or ambiguity -> Manual Review.

    TODO: "To Watch" and "manual-review" are outside the documented status set
    (inbox | kept | promoted | tossed). Kept for compatibility with the Movies
    Kanban and existing notes; decide whether Kanban column should live in a
    separate field instead.
    """
    if needs_manual_review or confidence < threshold or category == "Other / Ambiguous":
        return Route(resources_dir / MANUAL_REVIEW_SUBPATH, "manual-review", "Ambiguous")
    if category == "Movies & Shows" and is_movie_with_title:
        return Route(resources_dir / "Movies & Shows", "To Watch", "Movies & Shows")
    return Route(resources_dir / category, "inbox", category)
