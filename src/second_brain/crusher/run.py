"""Orchestrate ``second-brain crush`` (tiered, budget-capped, resumable).

Per card: cache check -> T0 metadata -> T1 captions -> T2 audio (if thin) ->
visual gate -> T3 keyframes (if needed) -> Gemini summary -> classifier
(Jev or Gemini fields) -> completeness check (escalate a tier and retry) ->
state/cache -> optional vault write.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field, replace
from pathlib import Path

from google.genai import errors as genai_errors
from pydantic import ValidationError

from ..config import CrusherSettings, Settings
from ..gemini import DailyQuotaExhaustedError, MissingApiKeyError, RateLimitExhaustedError
from ..io_utils import atomic_write_text
from ..urls import normalize_url
from ..vault import iter_cards, parse_frontmatter_strict
from .acquire import (
    TIER_VISUAL,
    AcquiredMedia,
    acquire_text,
    acquire_visuals,
    densify_keyframes,
)
from .analyze import AnalyzeContext, analyze_media
from .budget import BudgetManager, SpendCapReachedError
from .classify import Classifier, JevClassifier, build_classifier, build_state, merge_classification
from .gate import decide_visuals
from .lock import CrusherLock, CrusherLockError
from .postpass import run_postpass
from .review import classify_terminal
from .probe import ensure_ffmpeg
from .schema import CrusherAnalysis
from .state import (
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_FAILED_RETRYABLE,
    STATUS_MANUAL_REVIEW,
    STATUS_NEEDS_REVIEW,
    STATUS_UNAVAILABLE,
    CrusherState,
)
from .transcribe import WhisperTranscriber
from .verify import check_completeness
from .writer import write_card_updates
from .ytdlp import PermanentMediaError, TransientFetchError, YtDlpError, YtDlpRunner

logger = logging.getLogger(__name__)

TIER_NAMES = {0: "metadata", 1: "captions", 2: "audio", 3: "visual"}

# Errors that end the whole run (state is preserved; the next run resumes).
_RUN_STOPPERS = (DailyQuotaExhaustedError, RateLimitExhaustedError, SpendCapReachedError, MissingApiKeyError)


@dataclass
class CrushOptions:
    apply: bool = False
    limit: int | None = None
    folder: str | None = None
    url: str | None = None
    reprocess: bool = False
    write_children: bool = True
    max_spend: float | None = None  # overrides crusher.max_spend_usd for this run


@dataclass
class CrushResult:
    scanned: int = 0
    analyzed: int = 0
    written: int = 0
    skipped: int = 0
    unavailable: int = 0
    retryable: int = 0
    needs_review: int = 0
    manual_review: int = 0
    stopped_reason: str | None = None
    tiers: Counter = field(default_factory=Counter)
    spend_summary: str = ""
    report_lines: list[str] = field(default_factory=list)


@dataclass
class _Deps:
    """Per-run collaborators, built once so models/limiters are shared across cards."""

    cs: CrusherSettings
    state: CrusherState
    budget: BudgetManager
    runner: YtDlpRunner
    transcriber: WhisperTranscriber
    classifier: Classifier
    taxonomy: list[str]


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
    # Child item notes carry crusher_item_id + source_url; they are outputs, not inputs.
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


def _note_manual_review(settings: Settings, card, url: str, reason: str) -> None:
    """Append one dead-end item to the manual-review list. Idempotent per URL."""
    path = settings.crusher_manual_review_path
    existing = path.read_text(encoding="utf-8") if path.is_file() else (
        "# Crusher manual review\n\n"
        "The crusher could not analyze these after every tier and retry. "
        "Watch them yourself; `--reprocess` tries again.\n\n"
    )
    if url in existing:
        return
    reason = " ".join(reason.split())[:300]
    atomic_write_text(path, existing + f"- `{card.path.name}` `{url}` — {reason}\n")


def _escalate_visuals(media: AcquiredMedia, deps: _Deps, reasons: list[str]) -> bool:
    """Run T3. Returns False if visuals could not be fetched (item continues text-only)."""
    logger.info("Visual tier for %s: %s", media.url, "; ".join(reasons))
    try:
        acquire_visuals(media, deps.cs, deps.runner)
    except YtDlpError as exc:
        # TODO: if this is common in reports, record a per-URL "visual failed" stage so the
        # next run retries only T3 instead of the whole item.
        logger.warning("Visual tier failed for %s (continuing text-only): %s", media.url, exc)
        return False
    return bool(media.keyframe_paths or media.carousel_image_paths or media.video_path)


def _summarize_with_escalation(
    media: AcquiredMedia, ctx: AnalyzeContext, deps: _Deps
) -> tuple[CrusherAnalysis, int, bool]:
    """Return (analysis, passes, complete). Raises API/budget errors to the caller.

    Retries move to a richer input tier (text -> frames -> denser frames)
    instead of re-sending the same input at a higher fps.
    """
    cs = deps.cs
    gate = decide_visuals(media, cs)
    if not gate.needs_visuals and isinstance(deps.classifier, JevClassifier):
        prob = deps.classifier.needs_visuals(build_state(media, current_category=ctx.current_category or ""))
        gate = decide_visuals(media, cs, jev_needs_visuals=prob)
    visual_ok = True
    if gate.needs_visuals:
        visual_ok = _escalate_visuals(media, deps, gate.reasons)

    passes = 0
    analysis: CrusherAnalysis | None = None
    complete = False
    while passes < cs.max_passes:
        passes += 1
        ctx.pass_index = passes - 1
        analysis = analyze_media(deps.budget, cs, media, ctx, deps.taxonomy)
        completeness = check_completeness(analysis, media)
        if passes >= cs.max_passes:
            complete = completeness.complete
            break
        if media.tier_reached < TIER_VISUAL and visual_ok:
            # Pre-summary rules already said "no visuals", so any reason now
            # comes from the text summary (short list, needs_visuals flag, gaps).
            post = decide_visuals(media, cs, analysis=analysis, retry_hint=completeness.retry_hint)
            if post.needs_visuals:
                ctx.missing_hint = completeness.retry_hint
                visual_ok = _escalate_visuals(media, deps, post.reasons)
                if visual_ok:
                    continue
        if completeness.complete:
            complete = True
            break
        ctx.missing_hint = completeness.retry_hint
        analysis.completeness_notes = completeness.retry_hint
        if media.carousel_image_paths:
            continue  # all slides already sent; retry once more with the hint
        if not densify_keyframes(media, cs):
            break  # nothing richer to send; accept and flag for review
    assert analysis is not None
    return analysis, passes, complete and visual_ok


def _process_card(card, settings: Settings, opts: CrushOptions, deps: _Deps, result: CrushResult):
    """Returns (analysis | None, crusher_status). Raises budget/quota stops."""
    url = card.url
    cs = deps.cs
    pv = cs.prompt_version

    if opts.reprocess:
        deps.state.clear_stages(url)
    try:
        media = acquire_text(url, cs, deps.runner, cache=deps.state, transcriber=deps.transcriber)
    except PermanentMediaError as exc:
        result.unavailable += 1
        deps.state.record(url, status=STATUS_UNAVAILABLE, prompt_version=pv, last_error=str(exc))
        result.report_lines.append(f"- UNAVAILABLE `{url}`: {exc}")
        return None, STATUS_UNAVAILABLE
    except TransientFetchError as exc:
        attempts = deps.state.attempts(url) + 1
        if attempts >= cs.unavailable_after_attempts:
            result.unavailable += 1
            deps.state.record(url, status=STATUS_UNAVAILABLE, prompt_version=pv, attempts=attempts, last_error=str(exc))
            result.report_lines.append(f"- UNAVAILABLE after {attempts} tries `{url}`: {exc}")
            return None, STATUS_UNAVAILABLE
        result.retryable += 1
        deps.state.record(url, status=STATUS_FAILED_RETRYABLE, prompt_version=pv, attempts=attempts, last_error=str(exc))
        result.report_lines.append(f"- RETRY LATER `{url}` (attempt {attempts}): {exc}")
        return None, STATUS_FAILED_RETRYABLE

    try:
        fm = parse_frontmatter_strict(card.path.read_text(encoding="utf-8")) or {}
        current_category = str(fm.get("category") or "")
        ctx = AnalyzeContext(
            current_category=current_category,
            caption=f"{media.title} {media.description}",
            creator=media.creator,
            subtitle_text=media.transcript_text,
        )
        try:
            analysis, passes, complete = _summarize_with_escalation(media, ctx, deps)
        except (genai_errors.APIError, ValidationError, TimeoutError, RuntimeError) as exc:
            if isinstance(exc, _RUN_STOPPERS):
                raise
            attempts = deps.state.attempts(url) + 1
            status = classify_terminal(
                failed=True,
                attempts=attempts,
                max_attempts=cs.manual_review_after_attempts,
                complete=False,
                flagged=False,
            )
            deps.state.record(url, status=status, prompt_version=pv, attempts=attempts, last_error=str(exc))
            if status == STATUS_MANUAL_REVIEW:
                result.manual_review += 1
                _note_manual_review(settings, card, url, str(exc))
                result.report_lines.append(f"- MANUAL REVIEW `{card.path.name}` `{url}`: {exc}")
            else:
                result.retryable += 1
                result.report_lines.append(f"- RETRY LATER `{url}` (attempt {attempts}): {exc}")
            return None, status

        classification = deps.classifier.classify(
            build_state(media, analysis, current_category=current_category), taxonomy=deps.taxonomy
        )
        flagged = merge_classification(analysis, classification, cs, deps.taxonomy)

        status = classify_terminal(
            failed=False,
            attempts=passes,
            max_attempts=cs.manual_review_after_attempts,
            complete=complete,
            flagged=flagged,
        )
        if status == STATUS_NEEDS_REVIEW:
            result.needs_review += 1
        tier = TIER_NAMES.get(media.tier_reached, str(media.tier_reached))
        result.tiers[tier] += 1
        deps.state.save_analysis(url, analysis, prompt_version=pv, passes=passes)
        deps.state.record(url, status=status, prompt_version=pv, attempts=passes)
        result.analyzed += 1
        result.report_lines.append(
            f"- {card.path.name}: **{analysis.category}** ({analysis.media_shape}) conf={analysis.confidence} "
            f"items={len(analysis.items)} tier={tier} transcript={media.transcript_source} "
            f"classifier={deps.classifier.name}" + (" REVIEW" if status == STATUS_NEEDS_REVIEW else "")
        )
        return analysis, status
    finally:
        media.cleanup()


def run_crush(settings: Settings, opts: CrushOptions | None = None) -> CrushResult:
    opts = opts or CrushOptions()
    result = CrushResult()
    settings.require_vault()
    ensure_ffmpeg()

    cs = settings.crusher
    if opts.max_spend is not None:
        cs = replace(cs, max_spend_usd=opts.max_spend)
    budget = BudgetManager(cs, settings.second_brain_dir / "crusher_usage.json")
    budget.retest_exhausted_keys()
    deps = _Deps(
        cs=cs,
        state=CrusherState(settings.crusher_state_path, settings.crusher_cache_dir),
        budget=budget,
        runner=YtDlpRunner(cs),
        transcriber=WhisperTranscriber(cs),
        classifier=build_classifier(cs, on_usage=budget.record_jev_usage),
        taxonomy=list(settings.folder_map.keys()),
    )
    pv = cs.prompt_version
    logger.info("Crusher classifier: %s", deps.classifier.name)

    try:
        with CrusherLock(settings.crusher_lock_path, stale_hours=cs.lock_stale_hours):
            cards = _collect_cards(settings, opts)
            result.scanned = len(cards)
            logger.info("Crusher: %d card(s) queued (%s).", len(cards), "apply" if opts.apply else "dry-run")

            # Once the quota or spend cap stops new Gemini calls, keep walking the
            # queue so cards that already have a cached analysis still get written.
            api_stopped = False
            for idx, card in enumerate(cards, 1):
                url = card.url
                if deps.state.should_skip(url, prompt_version=pv, reprocess=opts.reprocess):
                    result.skipped += 1
                    analysis = deps.state.load_analysis(url, prompt_version=pv)
                    prior = deps.state.get(url)
                    status = prior.status if prior else STATUS_DONE
                    if analysis is None:
                        continue
                elif api_stopped:
                    continue
                else:
                    try:
                        analysis, status = _process_card(card, settings, opts, deps, result)
                    except _RUN_STOPPERS as exc:
                        result.stopped_reason = str(exc)
                        api_stopped = True
                        continue
                    if analysis is None:
                        continue

                if opts.apply:
                    try:
                        write_card_updates(
                            settings,
                            card.path,
                            analysis,
                            prompt_version=pv,
                            crusher_status=status,
                            write_children=opts.write_children,
                        )
                        result.written += 1
                    except (OSError, ValueError) as exc:
                        logger.error("Write failed for %s: %s", card.path, exc)
                        result.report_lines.append(f"  - write error: {exc}")

                if idx % cs.batch_size == 0:
                    logger.info("Checkpoint: %d/%d cards. %s", idx, len(cards), budget.spend_summary())

            if opts.apply and result.written:
                run_postpass(settings)

    except CrusherLockError as exc:
        result.stopped_reason = str(exc)

    result.spend_summary = budget.spend_summary()
    if result.analyzed:
        tiers = ", ".join(f"{name}={count}" for name, count in sorted(result.tiers.items()))
        per_item = budget.cost.run_usd / result.analyzed
        result.report_lines.append(
            f"- RUN SUMMARY: tiers [{tiers}]; {result.spend_summary}; avg ${per_item:.5f}/item; "
            f"yt-dlp processes={deps.runner.calls}"
        )
    if result.report_lines:
        _append_report(settings.crusher_report_path, result.report_lines)
        logger.info("Report appended to %s", settings.crusher_report_path)

    logger.info(
        "Crusher finished: scanned=%d analyzed=%d written=%d skipped=%d unavailable=%d retryable=%d "
        "needs_review=%d manual_review=%d",
        result.scanned,
        result.analyzed,
        result.written,
        result.skipped,
        result.unavailable,
        result.retryable,
        result.needs_review,
        result.manual_review,
    )
    logger.info("Crusher %s", result.spend_summary)
    if result.stopped_reason:
        logger.warning("Crusher stopped early: %s", result.stopped_reason)
    elif not opts.apply and result.analyzed:
        logger.info("Dry-run only. Re-run with --apply to rewrite vault cards (cache reused).")
    return result
