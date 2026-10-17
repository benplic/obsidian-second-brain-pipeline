"""Dataview hubs for every taxonomy folder.

Stock hubs start with a marker comment. A file that still matches a previous
stock template is replaced. Anything else, including a hand-edited hub, is
left alone.
"""

from __future__ import annotations

from pathlib import Path

from ..io_utils import atomic_write_text

HUB_MARKER = "<!-- second-brain-hub:1 -->"

# Exact bodies shipped before the card contract. migrate-cards and ensure_hubs
# may replace these. Edited copies are not in this set.
PREVIOUS_MUSIC_HUB = """# Music Hub

Unarchived tracks from the last 7 days, for batch transfer to a streaming playlist.
Set `status` to `archived` after you move a track, or `tossed` to drop it.
Run `second-brain ledger-sync` before deleting tossed cards so they are never re-ingested.

## Recent tracks

```dataview
TABLE WITHOUT ID
  file.link AS "Track",
  creator AS "Curator",
  sound AS "Sound Info",
  elink(url, "Open") AS "URL"
FROM "3 - Resources/Music"
WHERE file.name != this.file.name
  AND status != "archived"
  AND status != "tossed"
  AND file.ctime >= date(today) - dur(7 days)
SORT file.ctime DESC
```
"""

PREVIOUS_FITNESS_HUB = """# Fitness Hub

Routines with `status: ready`. Check the movements off inside each note.

```dataview
TABLE WITHOUT ID
  file.link AS "Note",
  creator AS "Creator",
  summary AS "Summary",
  file.ctime AS "Added"
FROM "3 - Resources/Fitness & Health"
WHERE file.name != this.file.name AND status = "ready"
SORT file.ctime DESC
```
"""

PREVIOUS_RECIPES_HUB = """# Recipes Hub

Meals with `status: ready`. Check ingredients and steps off inside each note.

```dataview
TABLE WITHOUT ID
  file.link AS "Recipe",
  creator AS "Creator",
  summary AS "Summary",
  file.ctime AS "Added"
FROM "3 - Resources/Recipes & Food"
WHERE file.name != this.file.name AND status = "ready"
SORT file.ctime DESC
```
"""

PREVIOUS_HUBS: dict[str, str] = {
    "Music Hub.md": PREVIOUS_MUSIC_HUB,
    "Fitness Hub.md": PREVIOUS_FITNESS_HUB,
    "Recipes Hub.md": PREVIOUS_RECIPES_HUB,
}

MUSIC_HUB = f"""{HUB_MARKER}
# Music Hub

Unarchived tracks from the last 7 days, for batch transfer to a streaming playlist.
Set `stage` to `archived` after you move a track. Set `status` to `tossed` to drop one.
Run `second-brain ledger-sync` before deleting tossed cards so they are never re-ingested.

## Recent tracks

```dataview
TABLE WITHOUT ID
  file.link AS "Track",
  creator AS "Curator",
  sound AS "Sound Info",
  elink(url, "Open") AS "URL"
FROM "3 - Resources/Music"
WHERE file.name != this.file.name
  AND status != "tossed"
  AND stage != "archived"
  AND file.ctime >= date(today) - dur(7 days)
SORT file.ctime DESC
```
"""

FITNESS_HUB = f"""{HUB_MARKER}
# Fitness Hub

Workouts in this folder. `stage: ready` means the checklist was extracted.
Step 3 cards with `status: inbox` are listed too. Check movements off inside each note.

```dataview
TABLE WITHOUT ID
  file.link AS "Note",
  creator AS "Creator",
  stage AS "Stage",
  summary AS "Summary",
  file.ctime AS "Added"
FROM "3 - Resources/Fitness & Health"
WHERE file.name != this.file.name AND status != "tossed"
SORT file.ctime DESC
```
"""

RECIPES_HUB = f"""{HUB_MARKER}
# Recipes Hub

Meals in this folder. `stage: ready` means the checklist was extracted.
Older cards with `status: inbox` are listed too.

```dataview
TABLE WITHOUT ID
  file.link AS "Recipe",
  creator AS "Creator",
  stage AS "Stage",
  summary AS "Summary",
  file.ctime AS "Added"
FROM "3 - Resources/Recipes & Food"
WHERE file.name != this.file.name AND status != "tossed"
SORT file.ctime DESC
```
"""

_SPECIAL_FILES = {
    "Music": ("Music Hub.md", MUSIC_HUB),
    "Fitness & Health": ("Fitness Hub.md", FITNESS_HUB),
    "Recipes & Food": ("Recipes Hub.md", RECIPES_HUB),
}


def generic_hub(folder: str) -> str:
    title = folder
    return f"""{HUB_MARKER}
# {title} Hub

Triage cards here: set `status` to `kept`, `promoted`, or `tossed`.
`stage` is set by the pipeline (`ready`, `to-watch`, `manual-review`) and is blank when unused.
Run `second-brain ledger-sync` before deleting tossed cards so they are never re-ingested.

## Cards

```dataview
TABLE WITHOUT ID
  file.link AS "Card",
  creator AS "Creator",
  status AS "Status",
  stage AS "Stage",
  file.ctime AS "Added"
FROM "3 - Resources/{folder}"
WHERE file.name != this.file.name AND status != "tossed"
SORT file.ctime DESC
```
"""


def hub_for(folder: str) -> tuple[str, str]:
    if folder in _SPECIAL_FILES:
        return _SPECIAL_FILES[folder]
    return f"{folder} Hub.md", generic_hub(folder)


def ensure_hubs(resources_dir: Path, folder_map: dict[str, str] | None = None) -> None:
    """Create missing stock hubs. Replace a hub only when it is a known previous template."""
    folders = list(dict.fromkeys((folder_map or {}).values())) or list(_SPECIAL_FILES)
    for folder in folders:
        name, body = hub_for(folder)
        path = resources_dir / folder / name
        if not path.exists():
            atomic_write_text(path, body)
            continue
        try:
            current = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if current == body:
            continue
        previous = PREVIOUS_HUBS.get(name)
        if previous is not None and current == previous:
            atomic_write_text(path, body)
