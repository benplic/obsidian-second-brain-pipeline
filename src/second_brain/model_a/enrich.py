"""Model A enrich (formerly model_a_retroactive.py).

Re-classifies existing cards in Tech & Coding, Project Ideas, Movies & Shows
and Inbox/Manual Review, 10 per Gemini request: adds criteria tags, renames the
file to a useful title, routes ambiguous cards to Manual Review and adds movies
to the ``!Watchlist Kanban.md`` "To Watch" column.

Changes vs the original script (all data-preserving):
  * a failed batch leaves its cards untouched. The original wrote fallback
    cards titled "API Rate Limit Exceeded", moved them to Manual Review and
    deleted the originals;
  * results are matched by ``item_id`` instead of assuming response order;
  * the user's notes section and triage status (kept/promoted/tossed) survive
    the rewrite;
  * a URL merged into an existing movie note is recorded in the ledger, since
    it no longer appears in any frontmatter;
  * the metadata snapshot is optional; without it the card's own title and
    summary are sent.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

from google.genai import errors as genai_errors
from pydantic import BaseModel, Field, ValidationError

from ..config import Settings
from ..gemini import DailyQuotaExhaustedError, RateLimitExhaustedError, call_with_backoff, create_client
from ..io_utils import atomic_write_text
from ..ledger import EVENT_CARDED, EVENT_MERGED, UrlLedger
from ..urls import normalize_url
from ..vault import parse_frontmatter, sanitize_filename, split_frontmatter, yaml_str
from .common import HUMAN_STATUSES, KANBAN_FILENAME, MANUAL_REVIEW_SUBPATH, MODEL_A_CATEGORIES, CategoryType, route

logger = logging.getLogger(__name__)

_NOTES_HEADING_RE = re.compile(r"^## (Notes & Insights|Actionable Notes)\s*$", re.MULTILINE)
_H1_RE = re.compile(r"^# (.+)$", re.MULTILINE)
_SUMMARY_RE = re.compile(r"^> \*\*(?:Summary|Context):\*\*\s*(.+)$", re.MULTILINE)
DEFAULT_NOTES = "## Actionable Notes\n- [ ] Review core concept\n- \n"


class VideoAnalysis(BaseModel):
    item_id: str = Field(description="The exact ID passed in the request.")
    category: CategoryType = Field(description="Target category for Model A.")
    summary: str = Field(description="5-10 word actionable summary. This will be used as the file name.")
    confidence: float = Field(description="Confidence score from 0.0 to 1.0.")
    needs_manual_review: bool = Field(description="True if context remains too ambiguous.")
    extracted_movie_title: str | None = Field(default=None, description="Exact name of the movie/show. Null if not.")
    movie_type: str | None = Field(default=None, description="e.g., Movie, TV Show, Anime, Documentary.")
    movie_genre: str | None = Field(default=None, description="e.g., Sci-Fi, Horror, Comedy, Drama.")
    tech_category: str | None = Field(default=None, description="e.g., Hardware, Software, Programming, Setup.")
    tech_tool: str | None = Field(default=None, description="e.g., Python, Blender, React, Keyboard.")
    project_field: str | None = Field(default=None, description="e.g., Woodworking, Coding, Electronics, DIY.")
    project_time: str | None = Field(default=None, description="e.g., Quick, Weekend, Long-term.")


class BatchVideoAnalysis(BaseModel):
    items: list[VideoAnalysis]


@dataclass
class CardToEnrich:
    path: Path
    url: str
    content: str


@dataclass
class EnrichResult:
    enriched: int = 0
    merged: int = 0
    skipped: int = 0
    stopped_reason: str | None = None


def load_metadata_snapshot(path: Path | None) -> dict[str, dict]:
    """Optional url -> metadata map from an old clean_metadata.json export."""
    if not path:
        return {}
    if not path.is_file():
        logger.warning("Metadata snapshot not found (continuing without it): %s", path)
        return {}
    try:
        items = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        logger.warning("Metadata snapshot is not valid JSON (continuing without it): %s", exc)
        return {}
    return {normalize_url(v.get("url", "")): v for v in items if isinstance(v, dict)}


def collect_cards(settings: Settings) -> list[CardToEnrich]:
    res = settings.resources_dir
    folders = [res / "Tech & Coding", res / "Project Ideas", res / "Movies & Shows", res / MANUAL_REVIEW_SUBPATH]
    cards: list[CardToEnrich] = []
    for folder in folders:
        if not folder.is_dir():
            continue
        for path in sorted(folder.rglob("*.md")):
            if path.name.endswith("Hub.md") or path.name.endswith("Kanban.md"):
                continue
            try:
                content = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                logger.warning("Skipping unreadable note %s: %s", path, exc)
                continue
            fm = parse_frontmatter(content)
            # Already enriched unless confidence is missing or the 0.0 fallback.
            if "confidence" in fm and str(fm.get("confidence")) not in {"0", "0.0"}:
                continue
            url = fm.get("url")
            if isinstance(url, str) and url.strip():
                cards.append(CardToEnrich(path=path, url=url.strip(), content=content))
    return cards


def build_payload(cards: list[CardToEnrich], snapshot: dict[str, dict]) -> list[dict]:
    payload = []
    for idx, card in enumerate(cards):
        local = snapshot.get(normalize_url(card.url))
        if local:
            title, desc, tags = local.get("title", ""), local.get("description", ""), local.get("tags", [])
        else:
            _, body = split_frontmatter(card.content)
            h1, summ = _H1_RE.search(body), _SUMMARY_RE.search(body)
            title = h1.group(1).strip() if h1 else "Unknown Title"
            desc = summ.group(1).strip() if summ else ""
            tags = []
        payload.append({"item_id": str(idx), "title": title or "Unknown Title", "description": desc, "tags": tags})
    return payload


def analyze_batch(client, settings: Settings, payload: list[dict]) -> dict[str, VideoAnalysis]:
    from google.genai import types

    prompt = (
        "Analyze these saved videos for an Obsidian Second Brain under Model A.\n"
        f"Target Categories: {MODEL_A_CATEGORIES}.\n\n"
        f"{json.dumps(payload, indent=2)}\n\n"
        "For each video, return the analysis matching its 'item_id'. Extract appropriate criteria tags. "
        "If a video does not fit Tech, Project Ideas, or Movies, flag needs_manual_review=True."
    )
    response = call_with_backoff(
        lambda: client.models.generate_content(
            model=settings.gemini.model, contents=prompt,
            config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=BatchVideoAnalysis),
        ),
        max_retries=settings.gemini.max_retries, base_seconds=settings.model_a.backoff_base_seconds,
        jitter=settings.gemini.jitter_seconds,
    )
    parsed = BatchVideoAnalysis.model_validate_json(response.text or "")
    return {item.item_id: item for item in parsed.items}


def format_tags(analysis: VideoAnalysis, category_label: str) -> str:
    tags = ["second-brain", f"category/{category_label.lower().replace(' ', '-')}"]

    def add(prefix: str, value: str | None) -> None:
        if value:
            tags.append(f"{prefix}/{value.lower().replace(' ', '-').replace('/', '')}")

    if analysis.category == "Movies & Shows":
        add("type", analysis.movie_type)
        add("genre", analysis.movie_genre)
    elif analysis.category == "Tech & Coding":
        add("tech", analysis.tech_category)
        add("tool", analysis.tech_tool)
    elif analysis.category == "Project Ideas":
        add("field", analysis.project_field)
        add("time", analysis.project_time)
    return "\n".join(f"  - {tag}" for tag in tags)


def existing_notes_section(content: str) -> str:
    """User-written notes (from the notes heading to the end), or the default block."""
    _, body = split_frontmatter(content)
    match = _NOTES_HEADING_RE.search(body)
    return body[match.start():].rstrip() + "\n" if match else DEFAULT_NOTES


def add_to_kanban(resources_dir: Path, movie_title: str) -> None:
    kanban_path = resources_dir / "Movies & Shows" / KANBAN_FILENAME
    if not kanban_path.exists():
        return
    content = kanban_path.read_text(encoding="utf-8")
    if f"[[{movie_title}]]" in content:
        return
    heading = "## To Watch\n"
    if heading not in content:
        logger.warning("Kanban has no '## To Watch' column; skipped '%s'.", movie_title)
        return
    atomic_write_text(kanban_path, content.replace(heading, heading + f"- [ ] [[{movie_title}]]\n", 1))
    logger.info("    -> Added '%s' to Kanban To Watch.", movie_title)


def _target_path(target_dir: Path, stem: str, original: Path) -> Path:
    candidate, counter = target_dir / f"{stem}.md", 2
    while candidate.exists() and candidate != original:
        candidate = target_dir / f"{stem} ({counter}).md"
        counter += 1
    return candidate


def apply_analysis(settings: Settings, ledger: UrlLedger, card: CardToEnrich, analysis: VideoAnalysis,
                   snapshot: dict[str, dict]) -> str:
    """Rewrite/move/merge one card. Returns "enriched" or "merged"."""
    local = snapshot.get(normalize_url(card.url), {})
    old_fm = parse_frontmatter(card.content)
    creator = local.get("creator") or old_fm.get("creator") or "Unknown"
    is_movie = analysis.category == "Movies & Shows" and bool(analysis.extracted_movie_title)
    r = route(settings.resources_dir, analysis.category, analysis.confidence, analysis.needs_manual_review,
              settings.model_a.confidence_threshold, is_movie_with_title=is_movie)
    title_source = analysis.extracted_movie_title if (is_movie and r.status == "To Watch") else analysis.summary
    final_title = sanitize_filename(title_source, max_len=75, fallback="Untitled")
    r.target_dir.mkdir(parents=True, exist_ok=True)
    new_path = r.target_dir / f"{final_title}.md"

    # Movies: several clips of one film collapse into a single note.
    if is_movie and r.status == "To Watch" and new_path.exists() and new_path != card.path:
        with open(new_path, "a", encoding="utf-8") as handle:
            handle.write(f"- Clip/Trailer: [{card.url}]({card.url}) - @{creator}\n")
            handle.flush()
            os.fsync(handle.fileno())
        # Record before deleting: once merged, the URL is only in a note body.
        ledger.record(card.url, EVENT_MERGED, into=new_path.name, source="model-a-enrich")
        # TODO: user notes on the merged-away card are dropped (as in the original); consider appending them.
        card.path.unlink(missing_ok=True)
        add_to_kanban(settings.resources_dir, final_title)
        return "merged"

    new_path = _target_path(r.target_dir, final_title, card.path)
    old_status = str(old_fm.get("status") or "")
    status = old_status if old_status.lower() in HUMAN_STATUSES else r.status
    content = f"""---
category: {yaml_str(r.category_label)}
creator: {yaml_str(creator)}
url: {yaml_str(card.url)}
status: {yaml_str(status)}
confidence: {analysis.confidence}
tags:
{format_tags(analysis, r.category_label)}
---

# {final_title}

> **Context:** {analysis.summary}

- **Creator:** @{creator}
- **Source Link:** [{card.url}]({card.url})

{existing_notes_section(card.content)}"""
    atomic_write_text(new_path, content)
    ledger.record(card.url, EVENT_CARDED, category=r.category_label, source="model-a-enrich")
    if new_path != card.path:
        card.path.unlink(missing_ok=True)
    logger.info("  -> Saved as %s [%s]", new_path.name, r.category_label)
    if is_movie and r.status == "To Watch":
        add_to_kanban(settings.resources_dir, final_title)
    return "enriched"


def run_enrich(settings: Settings, client=None) -> EnrichResult:
    result = EnrichResult()
    settings.require_vault()
    cards = collect_cards(settings)
    logger.info("Found %d card(s) to enrich.", len(cards))
    if not cards:
        return result
    snapshot = load_metadata_snapshot(settings.model_a.metadata_snapshot_path)
    ledger = UrlLedger(settings.ledger_path)
    client = client or create_client()
    size = settings.model_a.batch_size

    for start in range(0, len(cards), size):
        batch = cards[start:start + size]
        logger.info("--- Batch %d (items %d-%d) ---", start // size + 1, start + 1, start + len(batch))
        try:
            results = analyze_batch(client, settings, build_payload(batch, snapshot))
        except (DailyQuotaExhaustedError, RateLimitExhaustedError) as exc:
            result.stopped_reason = str(exc)
        except genai_errors.APIError as exc:
            result.stopped_reason = f"API error {exc.code}: {exc.message or exc}"
        except ValidationError as exc:
            result.stopped_reason = f"Unparseable Gemini response: {exc}"
        if result.stopped_reason:
            logger.error("Stopping enrich: %s. Unprocessed cards were left untouched.", result.stopped_reason)
            break
        for idx, card in enumerate(batch):
            analysis = results.get(str(idx))
            if analysis is None:
                logger.warning("  No analysis returned for %s; left untouched.", card.path.name)
                result.skipped += 1
                continue
            outcome = apply_analysis(settings, ledger, card, analysis, snapshot)
            if outcome == "merged":
                result.merged += 1
            else:
                result.enriched += 1
    logger.info("Enrich done: %d enriched, %d merged, %d skipped.", result.enriched, result.merged, result.skipped)
    return result
