"""Where a classified item is written.

``status`` on a new card is always the triage value ``inbox``. Pipeline state
lives in ``stage``. Folder names come from the taxonomy, not from free text.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..taxonomy import canonical_category, folder_for
from .schema import FITNESS, MODEL_C_CATEGORIES, MUSIC, RECIPES

MANUAL_REVIEW_SUBPATH = Path("Inbox") / "Manual Review"

# Statuses a person sets. The pipeline must not overwrite them.
HUMAN_STATUSES = {"kept", "promoted", "tossed"}


@dataclass(frozen=True)
class Placement:
    target_dir: Path
    category_label: str
    stage: str | None


def stage_for(category: str, *, usable: bool) -> str | None:
    """Pipeline stage after a category is already known.

    Music stays unstaged so the weekly hub can list it until you set
    ``stage: archived`` yourself. Fitness and recipes are ``ready`` when the
    extraction is usable. Anything else that is not usable is ``manual-review``.
    """
    category = canonical_category(category)
    if not usable:
        return "manual-review"
    if category in {FITNESS, RECIPES}:
        return "ready"
    if category == "Movies & Shows":
        return "to-watch"
    return None


def placement(
    resources_dir: Path,
    folder_map: dict[str, str],
    category: str,
    *,
    usable: bool,
) -> Placement:
    """Folder for one already-classified item. Low quality goes to Manual Review."""
    category = canonical_category(category)
    stage = stage_for(category, usable=usable)
    known = category if category in folder_map or category in MODEL_C_CATEGORIES else "Miscellaneous"
    if category not in folder_map and category not in {MUSIC, FITNESS, RECIPES, "Movies & Shows"}:
        known = "Miscellaneous"
    if stage == "manual-review":
        label = known if known in folder_map else "Miscellaneous"
        return Placement(resources_dir / MANUAL_REVIEW_SUBPATH, label, stage)
    label = category if category in folder_map else known
    return Placement(resources_dir / folder_for(label, folder_map), label, stage)
