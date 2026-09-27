"""Rewrite vault cards and optional child item notes from crusher analysis."""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ..config import Settings
from ..io_utils import atomic_write_text
from ..taxonomy import category_tag, folder_for
from ..vault import format_frontmatter, parse_frontmatter_strict, sanitize_filename, split_frontmatter
from .schema import CrusherAnalysis, ExtractedItem

logger = logging.getLogger(__name__)

CRUSHER_START = "<!-- crusher:start -->"
CRUSHER_END = "<!-- crusher:end -->"
_NOTES_HEADING_RE = re.compile(r"^## (Notes & Insights|Actionable Notes)\s*$", re.MULTILINE)
_HUMAN_STATUSES = {"kept", "promoted", "tossed"}

KIND_TO_CATEGORY = {
    "destination": "Travel",
    "album": "Saved Music",
    "song": "Saved Music",
    "movie": "Movies & Shows",
    "recipe": "Recipes & Food",
    "product": "Miscellaneous",
    "tool": "Tech & Coding",
    "workout": "Fitness & Health",
    "tip": "Study Tips",
    "other": "Miscellaneous",
}


@dataclass
class WriteOutcome:
    parent_path: Path
    moved: bool
    children_written: int
    recategorized: bool


def item_category(item: ExtractedItem, fallback: str) -> str:
    return KIND_TO_CATEGORY.get(item.kind, fallback)


def crusher_item_id(parent_url: str, item: ExtractedItem) -> str:
    raw = f"{parent_url}|{item.kind}|{item.name}|{item.position or ''}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def existing_notes_section(content: str) -> str:
    _, body = split_frontmatter(content)
    if CRUSHER_START in body:
        body = body.split(CRUSHER_START, 1)[0]
    match = _NOTES_HEADING_RE.search(body)
    if match:
        return body[match.start() :].rstrip() + "\n"
    return "## Notes & Insights\n- \n"


def format_findings_block(analysis: CrusherAnalysis) -> str:
    lines = [
        CRUSHER_START,
        "## Video Analysis",
        "",
        f"> **Summary:** {analysis.summary}",
        "",
        f"- **Media shape:** `{analysis.media_shape}`",
        f"- **Confidence:** {analysis.confidence}",
    ]
    if analysis.language:
        lines.append(f"- **Language:** {analysis.language}")
    if analysis.completeness_notes:
        lines.append(f"- **Completeness:** {analysis.completeness_notes}")
    if analysis.findings:
        lines.append("")
        lines.append("### Findings")
        for bullet in analysis.findings:
            lines.append(f"- {bullet.lstrip('- ').strip()}")
    if analysis.items:
        lines.append("")
        lines.append("### Extracted items")
        for item in analysis.items:
            label = item.name
            if item.location_name:
                label = f"{item.name} ({item.location_name})"
            lines.append(f"- [{item.kind}] {label}")
    lines.append(CRUSHER_END)
    return "\n".join(lines) + "\n"


def merge_frontmatter(
    old_fm: dict,
    analysis: CrusherAnalysis,
    *,
    prompt_version: str,
    crusher_status: str,
    apply_category: str,
) -> dict:
    merged = dict(old_fm)
    previous = str(old_fm.get("category") or "")
    if previous and previous != apply_category:
        merged["previous_category"] = previous
    merged["category"] = apply_category
    merged["summary"] = analysis.summary
    merged["media_shape"] = analysis.media_shape
    merged["confidence"] = analysis.confidence
    merged["crusher_version"] = prompt_version
    merged["crusher_status"] = crusher_status
    merged["analyzed_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    merged["items_count"] = len(analysis.items)
    tags = list(old_fm.get("tags") or [])
    if not isinstance(tags, list):
        tags = [str(tags)]
    for tag in ("saved-media", category_tag(apply_category)):
        if tag not in tags:
            tags.append(tag)
    merged["tags"] = tags
    return merged


def rebuild_body(old_content: str, analysis: CrusherAnalysis, *, title: str, creator: str, url: str) -> str:
    findings = format_findings_block(analysis)
    notes = existing_notes_section(old_content)
    return (
        f"# {title}\n\n"
        f"> **Summary:** {analysis.summary}\n\n"
        f"- **Creator:** @{creator}\n"
        f"- **Source:** [{url}]({url})\n\n"
        f"{findings}"
        f"{notes}"
    )


def child_frontmatter(
    parent_title: str,
    parent_url: str,
    item: ExtractedItem,
    category: str,
    creator: str,
    item_id: str,
) -> dict:
    fm: dict = {
        "category": category,
        "creator": creator,
        "source_url": parent_url,
        "parent": f"[[{parent_title}]]",
        "crusher_item_id": item_id,
        "item_kind": item.kind,
        "status": "inbox",
        "tags": ["saved-media", category_tag(category), f"crusher-item/{item.kind}"],
        "summary": item.name,
    }
    if item.kind in {"album", "song"} and item.artist:
        fm["artist"] = item.artist
    if item.kind == "destination":
        fm["location"] = ""
        fm["location_name"] = item.location_name or item.name
        fm["weight"] = 0
        fm["mapMarkerColor"] = "#9E9E9E"
    return fm


def render_child_note(fm: dict, item: ExtractedItem) -> str:
    title = sanitize_filename(item.name, max_len=70, fallback="Item")
    body = f"# {title}\n\n> **From parent video:** {fm.get('parent', '')}\n\n- **Item:** {item.name}\n"
    if item.notes:
        body += f"- **Notes:** {item.notes}\n"
    return f"---\n{format_frontmatter(fm)}\n---\n\n{body}"


def _target_path(target_dir: Path, stem: str, exclude: Path | None = None) -> Path:
    candidate = target_dir / f"{stem}.md"
    counter = 2
    while candidate.exists() and candidate != exclude:
        candidate = target_dir / f"{stem} ({counter}).md"
        counter += 1
    return candidate


def write_card_updates(
    settings: Settings,
    card_path: Path,
    analysis: CrusherAnalysis,
    *,
    prompt_version: str,
    crusher_status: str,
    write_children: bool,
) -> WriteOutcome:
    content = card_path.read_text(encoding="utf-8")
    old_fm = parse_frontmatter_strict(content)
    if old_fm is None:
        raise ValueError(f"Could not parse frontmatter for {card_path.name}")

    url = str(old_fm.get("url") or "")
    creator = str(old_fm.get("creator") or "Unknown")
    locked = bool(old_fm.get("category_locked"))
    old_category = str(old_fm.get("category") or "Miscellaneous")
    new_category = old_category
    if analysis.recategorize and not locked:
        new_category = analysis.category if analysis.category in settings.folder_map else "Miscellaneous"

    title = sanitize_filename(analysis.title or analysis.summary, max_len=75, fallback=card_path.stem)
    new_fm = merge_frontmatter(
        old_fm,
        analysis,
        prompt_version=prompt_version,
        crusher_status=crusher_status,
        apply_category=new_category,
    )
    body = rebuild_body(content, analysis, title=title, creator=creator, url=url)
    new_content = f"---\n{format_frontmatter(new_fm)}\n---\n\n{body}"

    target_dir = settings.resources_dir / folder_for(new_category, settings.folder_map)
    target_dir.mkdir(parents=True, exist_ok=True)
    dest = _target_path(target_dir, title, exclude=card_path if card_path.parent == target_dir else None)
    if dest != card_path:
        atomic_write_text(dest, new_content)
        card_path.unlink(missing_ok=True)
    else:
        dest = card_path
        atomic_write_text(dest, new_content)

    children = 0
    parent_folder_name = sanitize_filename(dest.stem, max_len=50)
    child_links: list[str] = []

    if write_children and analysis.items:
        for item in analysis.items:
            cat = item_category(item, new_category)
            base_dir = settings.resources_dir / folder_for(cat, settings.folder_map)
            child_dir = base_dir / parent_folder_name
            child_dir.mkdir(parents=True, exist_ok=True)
            item_id = crusher_item_id(url, item)
            existing: Path | None = None
            for path in child_dir.glob("*.md"):
                try:
                    fm = parse_frontmatter_strict(path.read_text(encoding="utf-8"))
                except OSError:
                    continue
                if fm and fm.get("crusher_item_id") == item_id:
                    existing = path
                    break
            if existing:
                fm = parse_frontmatter_strict(existing.read_text(encoding="utf-8")) or {}
                if str(fm.get("status", "")).lower() in _HUMAN_STATUSES:
                    child_links.append(f"- [[{existing.stem}]] ({item.kind})")
                    continue
            fm = child_frontmatter(dest.stem, url, item, cat, creator, item_id)
            stem = sanitize_filename(item.name, max_len=60, fallback="Item")
            child_path = child_dir / f"{stem}.md"
            if child_path.exists() and child_path != existing:
                child_path = _target_path(child_dir, stem)
            atomic_write_text(child_path, render_child_note(fm, item))
            children += 1
            child_links.append(f"- [[{child_path.stem}]] ({item.kind})")

    if child_links and "### Child items" not in dest.read_text(encoding="utf-8"):
        updated = dest.read_text(encoding="utf-8")
        append = "\n".join(["", "### Child items"] + child_links) + "\n"
        if CRUSHER_END in updated:
            updated = updated.replace(CRUSHER_END, CRUSHER_END + append, 1)
        else:
            updated += append
        atomic_write_text(dest, updated)

    return WriteOutcome(
        parent_path=dest,
        moved=dest != card_path,
        children_written=children,
        recategorized=new_category != old_category,
    )
