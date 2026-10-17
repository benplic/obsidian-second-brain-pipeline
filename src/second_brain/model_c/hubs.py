"""Dataview hub notes for the three Model C folders.

Written only when the file is missing, so a hand-edited hub is left alone.
"""

from __future__ import annotations

from pathlib import Path

from ..io_utils import atomic_write_text

MUSIC_HUB = """# Music Hub

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

FITNESS_HUB = """# Fitness Hub

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

RECIPES_HUB = """# Recipes Hub

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

# folder -> (filename, body). Keys match the route targets in common.py.
HUBS: dict[str, tuple[str, str]] = {
    "Music": ("Music Hub.md", MUSIC_HUB),
    "Fitness & Health": ("Fitness Hub.md", FITNESS_HUB),
    "Recipes & Food": ("Recipes Hub.md", RECIPES_HUB),
}


def ensure_hubs(resources_dir: Path) -> None:
    """Create any missing hub note. Existing files are not overwritten."""
    for folder, (name, body) in HUBS.items():
        path = resources_dir / folder / name
        if path.exists():
            continue
        atomic_write_text(path, body)
