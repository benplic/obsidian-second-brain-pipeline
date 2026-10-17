"""Markdown cards. Every writer uses this so status, stage, and tags match.

``status`` is triage (``inbox`` on create). ``stage`` is pipeline state and is
omitted when there is nothing to record.
"""

from __future__ import annotations

import json

from .model_b.schema import route_tag
from .model_c.schema import FITNESS, MUSIC, RECIPES
from .taxonomy import canonical_category, category_tag
from .vault import yaml_str

SAVED_MEDIA_TAG = "saved-media"
MODEL_A_CATEGORIES = ("Tech & Coding", "Project Ideas", "Movies & Shows")


def card_tags(category: str) -> list[str]:
    return [SAVED_MEDIA_TAG, category_tag(category)]


def _frontmatter(
    *,
    category: str,
    creator: str,
    url: str,
    stage: str | None,
    confidence: float | None,
    summary: str,
    artist: str | None = None,
    sound: str | None = None,
    extra: str = "",
) -> str:
    lines = [
        "---",
        f"category: {yaml_str(category)}",
        f"creator: {yaml_str(creator)}",
        f"url: {yaml_str(url)}",
        'status: "inbox"',
    ]
    if stage:
        lines.append(f"stage: {yaml_str(stage)}")
    if confidence is not None:
        lines.append(f"confidence: {confidence}")
    lines.append(f"summary: {yaml_str(summary)}")
    if artist is not None:
        lines.append(f"artist: {yaml_str(artist)}")
    if sound is not None:
        lines.append(f"sound: {yaml_str(sound)}")
    lines.append("tags:")
    lines.extend(f"  - {tag}" for tag in card_tags(category))
    if extra:
        lines.append(extra.rstrip("\n"))
    lines.append("---")
    return "\n".join(lines)


def checkbox_lines(items: list[str]) -> str:
    cleaned = [" ".join(item.split()) for item in items if item and str(item).strip()]
    if not cleaned:
        return "- [ ]"
    return "\n".join(f"- [ ] {item}" for item in cleaned)


def render_manual(url: str, *, summary: str, confidence: float = 0.0, category: str = "Miscellaneous") -> str:
    """Manual-review card. Category stays a taxonomy key, never a path."""
    category = canonical_category(category) or "Miscellaneous"
    body = _frontmatter(
        category=category,
        creator="Unknown",
        url=url,
        stage="manual-review",
        confidence=confidence,
        summary=summary,
    )
    return f"""{body}

# {summary}

> **Summary:** {summary}

- **Source Link:** [{url}]({url})

## Review
{summary}
"""


def render_model_c(
    *,
    category: str,
    title: str,
    creator: str,
    url: str,
    summary: str,
    items: list[str],
    stage: str | None,
    confidence: float | None,
    artist: str = "",
    sound: str = "",
) -> str:
    category = canonical_category(category)
    music = category == MUSIC
    fm = _frontmatter(
        category=category,
        creator=creator or "Unknown",
        url=url,
        stage=stage,
        confidence=confidence,
        summary=summary,
        artist=artist if music else None,
        sound=sound if music else None,
    )
    if category == FITNESS:
        section = f"## Exercises & Movement Routine\n{checkbox_lines(items)}\n"
    elif category == RECIPES:
        section = f"## Ingredients & Preparation\n{checkbox_lines(items)}\n"
    elif music:
        section = f"## Artist & Sound\n- **Artist:** {artist or 'Unknown'}\n- **Sound:** {sound or 'Unknown'}\n"
    else:
        section = f"## Review\n{summary}\n"
    return f"""{fm}

# {title or summary or "Untitled"}

> **Summary:** {summary}

- **Creator:** @{creator or "Unknown"}
- **Source Link:** [{url}]({url})

{section}"""


def render_model_b(
    *,
    category: str,
    title: str,
    creator: str,
    url: str,
    summary: str,
    destination: str | None,
    location_name: str | None,
    stage: str,
    confidence: float,
    location: tuple[float, float] | None = None,
    address: str | None = None,
) -> str:
    """Travel card. ``status`` stays ``inbox``; the pin outcome is ``stage``.

    ``location`` and ``address`` are omitted unless geocoding succeeded, so an
    unmapped card does not look like a pin. Map View weight fields are written
    only for cards that stay in the Travel folder.
    """
    # Lazy import: write_cards imports this module at load time.
    from .steps.write_cards import map_marker_color_for_weight

    category = canonical_category(category) or "Travel"
    creator = creator or "Unknown"
    lines = [
        "---",
        f"category: {yaml_str(category)}",
        f"creator: {yaml_str(creator)}",
        f"url: {yaml_str(url)}",
        'status: "inbox"',
        f"stage: {yaml_str(stage)}",
        f"confidence: {json.dumps(confidence)}",
        f"summary: {yaml_str(summary)}",
    ]
    if destination:
        lines.append(f"destination: {yaml_str(destination)}")
    if stage != "manual-review":
        if location_name:
            lines.append(f"location_name: {yaml_str(location_name)}")
        lines.append("weight: 0")
        lines.append(f"mapMarkerColor: {yaml_str(map_marker_color_for_weight(0))}")
    if location is not None:
        latitude, longitude = location
        lines.append(f"location: [{latitude:.6f}, {longitude:.6f}]")
    if address:
        lines.append(f"address: {yaml_str(address)}")
    lines.append("tags:")
    lines.extend(f"  - {tag}" for tag in [*card_tags(category), route_tag(stage)])
    lines.append("---")
    frontmatter = "\n".join(lines)
    heading = title or destination or summary or "Untitled"
    destination_line = f"- **Destination:** {destination}\n" if destination else ""
    return f"""{frontmatter}

# {heading}

> **Summary:** {summary}

- **Creator:** @{creator}
- **Source Link:** [{url}]({url})
{destination_line}
## Notes
{summary}
"""


def render_model_a(
    *,
    category: str,
    title: str,
    creator: str,
    url: str,
    summary: str,
    stage: str | None,
    confidence: float | None,
) -> str:
    category = canonical_category(category)
    fm = _frontmatter(
        category=category,
        creator=creator or "Unknown",
        url=url,
        stage=stage,
        confidence=confidence,
        summary=summary,
    )
    return f"""{fm}

# {title or summary or "Untitled"}

> **Summary:** {summary}

- **Creator:** @{creator or "Unknown"}
- **Source Link:** [{url}]({url})

## Actionable Notes
- [ ] Review core concept
"""
