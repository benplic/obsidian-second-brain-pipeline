"""Universal category hubs for folders that do not have a specialized model.

A hub is created only when ``<Folder> Hub.md`` is missing. ``--repair``
replaces a file only when its text still equals the previous stock template.
Hand-edited synthesis notes are left alone.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from ..io_utils import atomic_write_text
from ..model_c.hubs import generic_hub

logger = logging.getLogger(__name__)

# Distinct from Model C's ``second-brain-hub:1`` so migrate-cards does not
# treat a Model D hub as a Music / Fitness / Recipes template.
HUB_MARKER = "<!-- second-brain-hub:model-d:1 -->"
PREVIOUS_HUB_MARKER = "<!-- second-brain-hub:model-d:0 -->"

# These folders own a richer hub (Dataview playlist, checklists, or map).
# Scaffolding the generic layout here would block that writer, because it
# refuses to replace a hub that already exists.
MODEL_OWNED_FOLDERS = frozenset({
    "Music",
    "Fitness & Health",
    "Recipes & Food",
    "Travel",
})

_INBOX_FOLDER = "Inbox"


@dataclass
class ScaffoldResult:
    """What ``scaffold_hubs`` did. Paths are absolute hub files."""

    created: list[Path] = field(default_factory=list)
    repaired: list[Path] = field(default_factory=list)
    unchanged: list[Path] = field(default_factory=list)


def hub_filename(folder: str) -> str:
    return f"{folder} Hub.md"


def dataview_source(resources_subdir: str, folder: str) -> str:
    """Vault path safe to embed inside a Dataview double-quoted string."""
    raw = f"{resources_subdir}/{folder}"
    return raw.replace("\\", "\\\\").replace('"', '\\"')


def _live_query(source: str, *, exclude_stage: bool) -> str:
    # ``stage`` is included because the pipeline stores manual-review there
    # while ``status`` stays ``inbox``. A status-only filter would keep those
    # cards in the inspiration stream.
    stage_line = '\n  AND stage != "manual-review"' if exclude_stage else ""
    return f'''```dataview
TABLE WITHOUT ID
  file.link AS "Note",
  category AS "Category",
  status AS "Status",
  summary AS "Summary",
  creator AS "Creator"
FROM "{source}"
WHERE file.name != this.file.name
  AND status != "tossed"
  AND status != "manual-review"{stage_line}
SORT file.ctime DESC
```'''


def _manual_review_callout(source: str) -> str:
    """Collapsed callout. Every line is quoted so Dataview runs inside it."""
    query = f'''```dataview
TABLE WITHOUT ID
  file.link AS "Note",
  category AS "Category",
  status AS "Status",
  stage AS "Stage",
  summary AS "Summary",
  creator AS "Creator"
FROM "{source}"
WHERE status = "manual-review" OR stage = "manual-review"
SORT file.ctime DESC
```'''
    quoted = "\n".join(f"> {line}" if line else ">" for line in query.split("\n"))
    return f"""> [!warning]- Manual review
> Notes in this folder with `status: manual-review` or `stage: manual-review`.
>
{quoted}
"""


def _hub_body(folder: str, source: str, *, marker: str, exclude_stage: bool, include_callout: bool) -> str:
    callout = f"\n{_manual_review_callout(source)}" if include_callout else ""
    return f"""{marker}
# {folder} Hub

Triage cards here: set `status` to `kept`, `promoted`, or `tossed`.
`stage` is pipeline state (`ready`, `to-watch`, `manual-review`) and is blank when unused.
Run `second-brain ledger-sync` before deleting tossed cards so they are never re-ingested.

## Live Inspiration Stream

{_live_query(source, exclude_stage=exclude_stage)}

## Synthesis & Scrapbook

Recurring themes and pattern notes. Link cards with `[[Note Name]]`.

-

## Maintenance Triage Checklist

- [ ] Review notes still marked `status: inbox`
- [ ] Keep: set `status: kept` when the note stays in this category
- [ ] Promote: set `status: promoted` and move the note to `1 - Projects/<Project Name>/`
- [ ] Toss: set `status: tossed` to hide the note without breaking backlinks
- [ ] Run `second-brain ledger-sync` before deleting a tossed card
{callout}"""


def render_universal_hub(folder: str, resources_subdir: str = "3 - Resources") -> str:
    """Current stock hub. This is the string the scaffolder writes."""
    source = dataview_source(resources_subdir, folder)
    return _hub_body(
        folder,
        source,
        marker=HUB_MARKER,
        exclude_stage=True,
        include_callout=True,
    )


def render_previous_universal_hub(folder: str, resources_subdir: str = "3 - Resources") -> str:
    """Stock hub shipped before the manual-review callout and stage filter.

    ``--repair`` replaces a file only when it still matches this text, or the
    Model C generic hub that ``ensure_hubs`` writes for the same folder.
    """
    source = dataview_source(resources_subdir, folder)
    return _hub_body(
        folder,
        source,
        marker=PREVIOUS_HUB_MARKER,
        exclude_stage=False,
        include_callout=False,
    )


def previous_stock_bodies(folder: str, resources_subdir: str = "3 - Resources") -> tuple[str, ...]:
    """Exact texts ``--repair`` may replace.

    ``generic_hub`` is what ``second-brain run`` already plants. A one-line
    edit of either body must not match, so a scrapbook note is left alone.
    """
    return (
        render_previous_universal_hub(folder, resources_subdir),
        generic_hub(folder),
    )


def scaffold_hubs(
    resources_dir: Path,
    *,
    repair: bool = False,
    resources_subdir: str = "3 - Resources",
) -> ScaffoldResult:
    """Create missing hubs under ``resources_dir``.

    Only direct child folders are considered. ``Inbox``, dot-folders, and
    model-owned folders are skipped. Existing hubs are not rewritten unless
    ``repair`` is set and the file is still a known stock template.
    """
    result = ScaffoldResult()
    resources_dir = Path(resources_dir)
    if not resources_dir.is_dir():
        logger.info("No resources folder at %s", resources_dir)
        return result

    for child in sorted(resources_dir.iterdir(), key=lambda path: path.name.lower()):
        if not child.is_dir():
            continue
        name = child.name
        if name.startswith(".") or name == _INBOX_FOLDER:
            logger.debug("Skipping %s", child)
            continue
        if name in MODEL_OWNED_FOLDERS:
            logger.debug("Skipping model-owned folder %s", name)
            continue

        path = child / hub_filename(name)
        body = render_universal_hub(name, resources_subdir)
        if not path.exists():
            atomic_write_text(path, body)
            logger.info("Created hub %s", path)
            result.created.append(path)
            continue

        try:
            current = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            logger.warning("Skipping unreadable hub %s: %s", path, exc)
            result.unchanged.append(path)
            continue

        if current == body:
            result.unchanged.append(path)
            continue

        if repair and current in previous_stock_bodies(name, resources_subdir):
            atomic_write_text(path, body)
            logger.info("Repaired stock hub %s", path)
            result.repaired.append(path)
            continue

        logger.info("Leaving existing hub %s", path)
        result.unchanged.append(path)

    return result
