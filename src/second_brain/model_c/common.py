"""Folder and status rules for Model C.

The three usable categories land in fixed folders. Anything else, including a
low extraction confidence, goes to Manual Review. Folder names are not taken
from model text beyond the closed category set, so a hallucinated path cannot
escape ``3 - Resources``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .schema import AMBIGUOUS, FITNESS, MUSIC, RECIPES

MANUAL_REVIEW_SUBPATH = Path("Inbox") / "Manual Review"

# TODO: "ready" and "manual-review" sit outside inbox | kept | promoted | tossed.
# The README migration note wants status kept to that set. Fitness and Recipes
# use "ready" because their hubs list notes you can act on immediately.


@dataclass(frozen=True)
class Route:
    target_dir: Path
    status: str
    category_label: str


def route(
    resources_dir: Path,
    category: str,
    confidence: float,
    needs_manual_review: bool,
    threshold: float,
) -> Route:
    """Pick the folder and status for one analysis.

    Music stays ``inbox`` so the weekly hub can batch-transfer tracks.
    Fitness and Recipes are ``ready`` because their checklists are usable as written.
    """
    unsure = needs_manual_review or confidence < threshold or category == AMBIGUOUS
    if unsure or category not in {MUSIC, FITNESS, RECIPES}:
        return Route(resources_dir / MANUAL_REVIEW_SUBPATH, "manual-review", "Ambiguous")
    if category == MUSIC:
        return Route(resources_dir / MUSIC, "inbox", MUSIC)
    if category == FITNESS:
        return Route(resources_dir / FITNESS, "ready", FITNESS)
    return Route(resources_dir / RECIPES, "ready", RECIPES)
