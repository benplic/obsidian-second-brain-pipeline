"""Orchestrate ``second-brain crush``."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from google.genai import errors as genai_errors
from pydantic import ValidationError

from ..config import Settings
from ..gemini import DailyQuotaExhaustedError, RateLimitExhaustedError
from ..io_utils import atomic_write_text
from ..urls import normalize_url
from ..vault import iter_cards, parse_frontmatter_strict
from .acquire import MediaUnavailableError, acquire_media
from .analyze import AnalyzeContext, analyze_media
from .budget import BudgetManager
from .lock import CrusherLock, CrusherLockError
from .postpass import run_postpass
from .probe import ensure_ffmpeg
from .schema import CrusherAnalysis
from .state import STATUS_DONE, STATUS_FAILED, STATUS_NEEDS_REVIEW, STATUS_UNAVAILABLE, CrusherState
from .verify import check_completeness
from .writer import write_card_updates

logger = logging.getLogger(__name__)


@dataclass
class CrushOptions:
    apply: bool = False
    limit: int | None = None
    folder: str | None = None
    url: str | None = None
    reprocess: bool = False
    write_children: bool = True


@dataclass
class CrushResult:
    scanned: int = 0
    analyzed: int = 0
    written: int = 0
    skipped: int = 0
    unavailable: int = 0
    needs_review: int = 0
    stopped_reason: str | None = None
    report_lines: list[str] = field(default_factory=list)


def _card_allowed(card, settings: Settings, opts: CrushOptions) -> bool:
    if opts.url and normalize_url(card.url) != normalize_url(opts.url):
        return False
    rel = card.path.relative_to(settings.resources_dir)
    if opts.folder and not str(rel).startswith(opts.folder.strip("/\\")):
        return False
    if settings.crusher.include_folders:
        if not any(str(rel).startswith(f.strip("/\\")) for f in settings.crusher.include_folders):
            return False
    if card.status.lower() in {s.lower() for s in settings.crusher.skip_statuses}:
        return False
    # Skip child item notes (they use source_url, not url registry entries — but some may lack url).
    try:
        content = card.path.read_text(encoding="utf-8")
        fm = parse_frontmatter_strict(content) or {}
        if fm.get("crusher_item_id") and not fm.get("url"):
            return False
    except OSError:
        return False
    return True


def _collect_cards(settings: Settings, opts: CrushOptions) -> list:
    cards = [c for c in iter_cards(settings.resources_dir) if _card_allowed(c, settings, opts)]
    cards.sort(key=lambda c: c.path.as_posix())
    if opts.limit is not None:
        cards = cards[: max(0, opts.limit)]
    return cards


def _append_report(path: Path, lines: list[str]) -> None:
    existing = path.read_text(encoding="utf-8") if path.is_file() else "# Crusher report\n\n"
    atomic_write_text(path, existing + "\n".join(lines) + "\n")


def run_crush(settings: Settings, opts: CrushOptions | None = None) -> CrushResult:
    opts = opts or CrushOptions()
    result = CrushResult()
    settings.require_vault()
    ensure_ffmpeg()

    state = CrusherState(settings.crusher_state_path, settings.crusher_cache_dir)
    budget = BudgetManager(settings.crusher, settings.second_brain_dir / "crusher_usage.json")
    taxonomy_keys = list(settings.folder_map.keys())
    prompt_version = settings.crusher.prompt_version

    try:
        with CrusherLock(settings.crusher_lock_path, stale_hours=settings.crusher.lock_stale_hours):
            cards = _collect_cards(settings, opts)
            result.scanned = len(cards)
            logger.info("Crusher: %d card(s) queued (%s).", len(cards), "apply" if opts.apply else "dry-run")

            for idx, card in enumerate(cards, 1):
                url = card.url
                analysis: CrusherAnalysis | None = None
                crusher_status = STATUS_DONE

                if state.should_skip(url, prompt_version=prompt_version, reprocess=opts.reprocess):
                    analysis = state.load_analysis(url, prompt_version=prompt_version)
                    if analysis is None:
                        result.skipped += 1
                        continue
                    result.skipped += 1
                else:
                    try:
                        media = acquire_media(url, settings.crusher)
                    except MediaUnavailableError as exc:
                        result.unavailable += 1
                        state.record(url, status=STATUS_UNAVAILABLE, prompt_version=prompt_version, last_error=str(exc))
                        result.report_lines.append(f"- UNAVAILABLE `{url}`: {exc}")
                        continue

                    content = card.path.read_text(encoding="utf-8")
                    fm = parse_frontmatter_strict(content) or {}
                    ctx = AnalyzeContext(
                        current_category=str(fm.get("category") or ""),
                        caption=f"{media.title} {media.description}",
                        creator=media.creator,
                        subtitle_text=media.subtitle_text,
                    )
                    passes = 0
                    analysis = None
                    while passes < settings.crusher.max_passes:
                        passes += 1
                        ctx.pass_index = passes - 1
                        try:
                            client, key_var = budget.create_client()
                            analysis = analyze_media(
                                client, budget, key_var, settings.crusher, media, ctx, taxonomy_keys
                            )
                        except (DailyQuotaExhaustedError, RateLimitExhaustedError) as exc:
                            result.stopped_reason = str(exc)
                            break
                        except (genai_errors.APIError, ValidationError, TimeoutError) as exc:
                            state.record(
                                url,
                                status=STATUS_FAILED,
                                prompt_version=prompt_version,
                                attempts=passes,
                                last_error=str(exc),
                            )
                            result.report_lines.append(f"- FAILED `{url}`: {exc}")
                            analysis = None
                            break

                        completeness = check_completeness(analysis, media)
                        if completeness.complete:
                            break
                        ctx.missing_hint = completeness.retry_hint
                        analysis.completeness_notes = completeness.retry_hint

                    if result.stopped_reason:
                        break
                    if analysis is None:
                        continue

                    if not check_completeness(analysis, media).complete:
                        crusher_status = STATUS_NEEDS_REVIEW
                        result.needs_review += 1

                    state.save_analysis(url, analysis, prompt_version=prompt_version, passes=passes)
                    state.record(url, status=crusher_status, prompt_version=prompt_version, attempts=passes)
                    result.analyzed += 1

                line = (
                    f"- {card.path.name}: **{analysis.category}** ({analysis.media_shape}) "
                    f"conf={analysis.confidence} items={len(analysis.items)}"
                )
                result.report_lines.append(line)

                if opts.apply and analysis is not None:
                    try:
                        write_card_updates(
                            settings,
                            card.path,
                            analysis,
                            prompt_version=prompt_version,
                            crusher_status=crusher_status,
                            write_children=opts.write_children,
                        )
                        result.written += 1
                    except (OSError, ValueError) as exc:
                        logger.error("Write failed for %s: %s", card.path, exc)
                        result.report_lines.append(f"  - write error: {exc}")

                if idx % settings.crusher.batch_size == 0:
                    logger.info("Checkpoint: %d/%d cards processed.", idx, len(cards))

            if opts.apply and result.written:
                run_postpass(settings)

    except CrusherLockError as exc:
        result.stopped_reason = str(exc)

    if result.report_lines:
        _append_report(settings.crusher_report_path, result.report_lines)
        logger.info("Report appended to %s", settings.crusher_report_path)

    logger.info(
        "Crusher finished: scanned=%d analyzed=%d written=%d skipped=%d unavailable=%d needs_review=%d",
        result.scanned,
        result.analyzed,
        result.written,
        result.skipped,
        result.unavailable,
        result.needs_review,
    )
    if result.stopped_reason:
        logger.warning("Crusher stopped early: %s", result.stopped_reason)
    elif not opts.apply and result.analyzed:
        logger.info("Dry-run only. Re-run with --apply to rewrite vault cards (cache reused).")
    return result
