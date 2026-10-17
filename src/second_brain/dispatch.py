"""One queue: pending_links.txt. Classification chooses a transform; it does not write the note.

With a Jev key, each URL is enriched through the crusher's yt-dlp runner and
classified before a model extracts usable material. Without that key, the
existing mega-batch categorize step supplies the category and ``write-cards``
renders the matching note shape.

Transient download failures stay in the queue. Permanent failures and
unparseable model output become ``stage: manual-review`` and are popped.
A 429 still stops the run and leaves the rest of the queue.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from pydantic import ValidationError

from .config import Settings
from .crusher.classify import ClassificationResult, build_classifier
from .crusher.lock import CrusherLock
from .crusher.ytdlp import (
    PermanentMediaError,
    TransientFetchError,
    YtDlpRunner,
    classify_stderr,
    parse_last_json_line,
)
from .gemini import DailyQuotaExhaustedError, RateLimitExhaustedError
from .ledger import EVENT_CARDED, UrlLedger
from .llm.factory import build_runtime
from .llm.runtime import LlmRuntime
from .llm.types import CompletionRequest, UnsupportedFeatureError
from .model_a.common import MODEL_A_CATEGORIES
from .model_b.geocode import geocode_place, safe_geocode
from .model_b.ingest import analyze_travel
from .model_b.schema import STAGE_MANUAL, TRAVEL, place_query, route_travel
from .model_c.common import placement
from .model_c.ingest import extract_keyframes, transcript_needs_vision
from .model_c.schema import MODEL_C_CATEGORIES, ModelCExtraction
from .model_c.hubs import ensure_hubs
from .notes import render_manual, render_model_a, render_model_b, render_model_c
from .steps.categorize import run_categorize
from .steps.extract_metadata import _rewrite_pending, run_extract
from .steps.write_cards import run_write_cards
from .taxonomy import canonical_category, folder_for
from .urls import is_valid_http_url, normalize_url
from .vault import get_vault_urls, sanitize_filename, write_card

logger = logging.getLogger(__name__)


@dataclass
class DispatchResult:
    created: int = 0
    skipped_known: int = 0
    stopped_reason: str | None = None


def warn_legacy_queues(settings: Settings) -> None:
    """Old Model A / Model C inboxes are not consumed. Tell the user once."""
    pending = settings.pending_links_path.resolve()
    for path in (settings.model_a_inbox_path, settings.model_c_inbox_path):
        try:
            if path.resolve() == pending or not path.is_file():
                continue
            if path.read_text(encoding="utf-8").strip():
                logger.warning(
                    "Leaving %s unread. New URLs belong in %s.",
                    path,
                    settings.pending_links_path,
                )
        except OSError as exc:
            logger.warning("Could not inspect legacy queue %s: %s", path, exc)


def meta_from_info(url: str, info: dict) -> dict:
    tags = info.get("tags") or []
    if not isinstance(tags, list):
        tags = []
    return {
        "url": url,
        "title": info.get("title") or "",
        "description": info.get("description") or "",
        "tags": [str(tag) for tag in tags if tag],
        "creator": info.get("uploader") or info.get("artist") or "Unknown",
        "artist": info.get("artist") or "",
        "track": info.get("track") or "",
        "transcript": "",
    }


def acquire_metadata(settings: Settings, url: str, runner: YtDlpRunner | None = None) -> dict:
    """Tier-1 JSON via the paced runner. Raises permanent or transient errors."""
    runner = runner or YtDlpRunner(settings.crusher)
    result = runner.run(["--dump-single-json", "--skip-download", "--ignore-errors"], url)
    info = parse_last_json_line(result.stdout)
    if info is None:
        kind = classify_stderr(result.stderr)
        detail = (result.stderr or "no metadata").strip()[:300]
        if kind is PermanentMediaError:
            raise PermanentMediaError(detail)
        raise TransientFetchError(detail or "metadata fetch failed")
    return meta_from_info(url, info)


def _state_text(meta: dict) -> str:
    tags = meta.get("tags") or []
    return "\n".join([
        f"Title: {meta.get('title') or ''}",
        f"Creator: {meta.get('creator') or ''}",
        f"Caption: {meta.get('description') or ''}",
        f"Hashtags: {', '.join(str(tag) for tag in tags[:20])}",
        f"Transcript: {meta.get('transcript') or 'none'}",
    ])


def _fill_music(meta: dict, extraction: ModelCExtraction) -> ModelCExtraction:
    if not (extraction.artist or "").strip() and meta.get("artist"):
        extraction.artist = str(meta["artist"])
    if not (extraction.sound or "").strip() and meta.get("track"):
        extraction.sound = str(meta["track"])
    return extraction


def extract_model_c(runtime: LlmRuntime, meta: dict, category: str, url: str) -> ModelCExtraction:
    """Ask only for usable material. The category is already decided."""
    tags = meta.get("tags") or []
    combined = f"{meta.get('title') or ''} {meta.get('description') or ''} {' '.join(tags)} {meta.get('transcript') or ''}"
    frames: list = []
    if transcript_needs_vision(combined):
        # On-screen lists (ingredients, movement names) are not in the caption.
        # Keyframes are optional; a missing extra is logged inside extract_keyframes.
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            frames = extract_keyframes(url, tmp)
    prompt = (
        f"This saved video is already classified as {category}. Do not choose a category.\n"
        "Return usable material only:\n"
        "- Saved Music: artist and track or sound lines\n"
        "- Fitness & Health: one exercise with reps or cues per item\n"
        "- Recipes & Food: ingredients and preparation steps\n"
        "Set needs_manual_review=true when the clip is too thin to use.\n"
        f"Title: {meta.get('title') or ''}\n"
        f"Caption: {meta.get('description') or ''}\n"
        f"Tags: {', '.join(str(tag) for tag in tags)}\n"
        f"Artist: {meta.get('artist') or 'Unknown'}\n"
        f"Track: {meta.get('track') or 'Unknown'}\n"
        f"Transcript: {meta.get('transcript') or 'N/A'}\n"
    )
    result = runtime.complete(CompletionRequest(
        prompt=prompt,
        response_schema=ModelCExtraction,
        extra_image_objects=frames,
    ))
    return _fill_music(meta, ModelCExtraction.model_validate_json(result.text or ""))


def _write_manual(settings: Settings, ledger: UrlLedger, known: set[str], url: str, summary: str, confidence: float, category: str) -> None:
    write_card(
        settings.resources_dir / "Inbox" / "Manual Review",
        sanitize_filename(summary, max_len=50, fallback="Manual review"),
        render_manual(url, summary=summary, confidence=confidence, category=category),
    )
    ledger.record(url, EVENT_CARDED, category=category, source="dispatch")
    known.add(url)


def _run_jev(
    settings: Settings,
    runtime: LlmRuntime,
    classifier,
    acquire,
    geocode,
) -> DispatchResult:
    result = DispatchResult()
    pending = settings.pending_links_path
    if not pending.exists():
        logger.info("Queue empty: '%s' not found.", pending.name)
        return result
    raw_lines = [ln.strip() for ln in pending.read_text(encoding="utf-8").splitlines() if ln.strip()]
    initial = set(raw_lines)
    if not raw_lines:
        return result

    ledger = UrlLedger(settings.ledger_path)
    known = get_vault_urls(settings.resources_dir) | ledger.urls
    keep: list[str] = []
    taxonomy = list(settings.folder_map)

    for index, line in enumerate(raw_lines):
        if not is_valid_http_url(line):
            logger.warning("Not an http(s) URL, left in the queue: %r", line)
            keep.append(line)
            continue
        url = normalize_url(line)
        if url in known:
            result.skipped_known += 1
            continue
        try:
            meta = acquire(url)
        except TransientFetchError as exc:
            logger.warning("Transient fetch, will retry %s: %s", url, exc)
            keep.append(line)
            continue
        except PermanentMediaError as exc:
            logger.info("Permanent failure for %s: %s", url, exc)
            _write_manual(settings, ledger, known, url, "Dead or unavailable link.", 0.0, "Miscellaneous")
            result.created += 1
            continue

        try:
            classified = classifier.classify(_state_text(meta), taxonomy=taxonomy)
        except (DailyQuotaExhaustedError, RateLimitExhaustedError) as exc:
            result.stopped_reason = str(exc)
            keep.append(line)
            keep.extend(raw_lines[index + 1:])
            break

        category, confidence = _classification(classified)
        threshold = settings.crusher.jev_min_confidence
        if not category or confidence < threshold:
            _write_manual(
                settings, ledger, known, url,
                "Classification confidence was too low to choose a folder.",
                confidence, category or "Miscellaneous",
            )
            result.created += 1
            continue

        category = canonical_category(category)
        try:
            if category in MODEL_C_CATEGORIES:
                extraction = extract_model_c(runtime, meta, category, url)
                usable = extraction.confidence >= settings.model_c.confidence_threshold and not extraction.needs_manual_review
                where = placement(settings.resources_dir, settings.folder_map, category, usable=usable)
                artist = extraction.artist or meta.get("artist") or ""
                sound = extraction.sound or meta.get("track") or ""
                body = render_model_c(
                    category=where.category_label,
                    title=meta.get("title") or extraction.summary,
                    creator=meta.get("creator") or "Unknown",
                    url=url,
                    summary=extraction.summary,
                    items=extraction.actionable_items,
                    stage=where.stage,
                    confidence=extraction.confidence,
                    artist=artist,
                    sound=sound,
                )
                stem = meta.get("title") or extraction.summary
            elif category == TRAVEL:
                extraction = analyze_travel(runtime, settings, meta, url)
                threshold = settings.model_b.confidence_threshold
                query = place_query(extraction)
                # Skip Nominatim unless the clip is confident travel with a place.
                # A miss still files the card; it does not stay in the queue.
                hit = None
                if (
                    extraction.is_travel_related
                    and extraction.confidence >= threshold
                    and query
                    and settings.crusher.geocoder != "none"
                ):
                    hit = safe_geocode(geocode, query)
                stage = route_travel(extraction, geocoded=hit is not None, threshold=threshold)
                destination = " ".join((extraction.place_name or "").split()) or None
                body = render_model_b(
                    category=TRAVEL,
                    title=meta.get("title") or extraction.summary,
                    creator=meta.get("creator") or "Unknown",
                    url=url,
                    summary=extraction.summary,
                    destination=destination,
                    location_name=query if stage != STAGE_MANUAL else None,
                    stage=stage,
                    confidence=extraction.confidence,
                    location=(hit.latitude, hit.longitude) if hit is not None and stage != STAGE_MANUAL else None,
                    address=hit.address if hit is not None and stage != STAGE_MANUAL and hit.address else None,
                )
                stem = meta.get("title") or extraction.summary or destination or "Travel"
                if stage == STAGE_MANUAL:
                    target = settings.resources_dir / "Inbox" / "Manual Review"
                else:
                    target = settings.resources_dir / folder_for(TRAVEL, settings.folder_map)
                write_card(target, sanitize_filename(stem, max_len=50, fallback="Saved Video"), body)
                ledger.record(url, EVENT_CARDED, category=TRAVEL, source="dispatch")
                known.add(url)
                result.created += 1
                continue
            elif category in MODEL_A_CATEGORIES:
                where = placement(settings.resources_dir, settings.folder_map, category, usable=True)
                summary = (meta.get("description") or meta.get("title") or "Saved video").strip()
                body = render_model_a(
                    category=where.category_label,
                    title=meta.get("title") or summary,
                    creator=meta.get("creator") or "Unknown",
                    url=url,
                    summary=summary,
                    stage=where.stage,
                    confidence=confidence,
                )
                stem = meta.get("title") or summary
            else:
                where = placement(settings.resources_dir, settings.folder_map, category, usable=True)
                summary = (meta.get("description") or meta.get("title") or "Saved video").strip()
                from .steps.write_cards import render_card

                _, stem, body = render_card({
                    "Category": where.category_label,
                    "Summary": summary,
                    "Creator": meta.get("creator") or "Unknown",
                    "URL": url,
                    "Title": meta.get("title") or summary,
                    "Tags": ", ".join(meta.get("tags") or []),
                })
                where_dir = settings.resources_dir / folder_for(where.category_label, settings.folder_map)
                write_card(where_dir, sanitize_filename(stem, max_len=50, fallback="Saved Video"), body)
                ledger.record(url, EVENT_CARDED, category=where.category_label, source="dispatch")
                known.add(url)
                result.created += 1
                continue
        except (DailyQuotaExhaustedError, RateLimitExhaustedError) as exc:
            result.stopped_reason = str(exc)
            keep.append(line)
            keep.extend(raw_lines[index + 1:])
            break
        except (ValidationError, UnsupportedFeatureError) as exc:
            logger.warning("Unparseable model response for %s: %s", url, exc)
            _write_manual(settings, ledger, known, url, "The model response could not be parsed.", 0.0, category)
            result.created += 1
            continue

        write_card(where.target_dir, sanitize_filename(stem, max_len=50, fallback="Saved Video"), body)
        ledger.record(url, EVENT_CARDED, category=where.category_label, source="dispatch")
        known.add(url)
        result.created += 1

    _rewrite_pending(pending, initial, keep)
    if result.stopped_reason:
        logger.error("Stopping dispatch: %s. Remaining links stay in the queue.", result.stopped_reason)
    return result


def _classification(result: ClassificationResult | None) -> tuple[str, float]:
    if result is None or not result.category:
        return "", 0.0
    return str(result.category), float(result.category_confidence or 0.0)


def _run_batched(settings: Settings) -> DispatchResult:
    """No Jev key: mega-batch category, then the same note shapes as the Jev path."""
    logger.info("No Jev classifier. Using extract, categorize, and write-cards.")
    run_extract(settings)
    categorized = run_categorize(settings)
    written = run_write_cards(settings)
    return DispatchResult(created=written.created, skipped_known=written.skipped_known, stopped_reason=categorized.stopped_reason)


def run_dispatch(
    settings: Settings,
    runtime: LlmRuntime | None = None,
    *,
    acquire=None,
    classifier=None,
    geocode=None,
) -> DispatchResult:
    """Pop ``pending_links.txt``. Holds the pipeline lock for the whole run."""
    settings.require_vault()
    warn_legacy_queues(settings)
    ensure_hubs(settings.resources_dir, settings.folder_map)
    with CrusherLock(settings.pipeline_lock_path, stale_hours=settings.crusher.lock_stale_hours, kind="pipeline"):
        chosen = classifier if classifier is not None else build_classifier(settings.crusher)
        if getattr(chosen, "name", "") != "jev":
            return _run_batched(settings)
        runtime = runtime or build_runtime(settings)
        fetch = acquire or (lambda url: acquire_metadata(settings, url))
        # Default binds settings so tests can inject a geocoder with no network.
        locate = geocode or (lambda query: geocode_place(query, settings))
        return _run_jev(settings, runtime, chosen, fetch, locate)
