"""Step 2: clean_metadata.json -> organized_tiktoks.csv via LLM mega-batches.

Budget: the free tier allows ~20 requests/day, so items go 100 per request
with a compact output schema to stay under output-token limits.

Crash safety per batch:
    1. LLM call (with backoff on 429)
    2. append rows to the CSV + fsync
    3. pop the batch from clean_metadata.json (atomic rewrite)
A crash between 2 and 3 leaves the batch in both files; the start-of-run
reconciliation pops those items instead of paying for them again.
"""

from __future__ import annotations

import json
import logging
import random
import time
from dataclasses import dataclass
from typing import Callable

from pydantic import BaseModel, ValidationError

from ..config import Settings
from ..gemini import DailyQuotaExhaustedError, RateLimitExhaustedError
from ..io_utils import append_csv_rows, atomic_write_json, read_csv_rows, read_json_list, remove_if_exists
from ..llm.factory import build_runtime
from ..llm.runtime import LlmRuntime
from ..llm.types import CompletionRequest
from ..taxonomy import DEFAULT_CATEGORY
from ..urls import normalize_url

logger = logging.getLogger(__name__)

CSV_FIELDS = ["Category", "Summary", "Creator", "URL", "Title", "Tags"]
TITLE_CHARS = 70
CAPTION_CHARS = 90


class VideoClassification(BaseModel):
    """Compact on purpose: every output token counts against the limit.

    Field ``i`` matches the ``i`` key in the prompt payload (the original
    schema called it ``idx`` while the prompt said ``i``).
    """

    i: int
    cat: str
    summ: str


class MegaBatchResponse(BaseModel):
    results: list[VideoClassification]


class EmptyResponseError(ValueError):
    """LLM returned no text (e.g. blocked by safety filters)."""


@dataclass
class CategorizeResult:
    categorized: int = 0
    requests: int = 0
    reconciled: int = 0
    remaining: int = 0
    stopped_reason: str | None = None


def build_prompt(batch: list[dict], categories: list[str]) -> str:
    payload = [
        {"i": idx, "t": (v.get("title") or "")[:TITLE_CHARS], "c": (v.get("description") or "")[:CAPTION_CHARS]}
        for idx, v in enumerate(batch)
    ]
    return (
        "Categorize each video into exactly one category from: "
        f"[{', '.join(categories)}]. Provide a 4-8 word summary for each.\n\n"
        f"Input Data:\n{json.dumps(payload)}"
    )


def parse_response(text: str | None, batch: list[dict]) -> list[dict]:
    """Map an LLM response onto CSV rows, one per input item, in input order.

    Items the model skipped fall back to Miscellaneous rather than being lost.
    """
    if not text:
        raise EmptyResponseError("LLM returned an empty response")
    parsed = MegaBatchResponse.model_validate_json(text)
    by_index = {item.i: item for item in parsed.results}
    missing = [idx for idx in range(len(batch)) if idx not in by_index]
    if missing:
        logger.warning("LLM omitted %d of %d item(s); defaulting them to %s.", len(missing), len(batch), DEFAULT_CATEGORY)

    rows = []
    for idx, video in enumerate(batch):
        match = by_index.get(idx)
        rows.append({
            "Category": match.cat if match else DEFAULT_CATEGORY,
            "Summary": match.summ if match else "No summary",
            "Creator": video.get("creator", ""),
            "URL": video.get("url", ""),
            "Title": video.get("title", ""),
            "Tags": ", ".join(video.get("tags") or []),
        })
    return rows


def classify_batch(
    runtime: LlmRuntime,
    batch: list[dict],
    settings: Settings,
    *,
    sleep: Callable[[float], None],
    rng,
) -> list[dict]:
    prompt = build_prompt(batch, list(settings.folder_map))
    request = CompletionRequest(prompt=prompt, response_schema=MegaBatchResponse)
    result = runtime.complete(request, sleep=sleep, rng=rng)
    return parse_response(result.text, batch)


def _save_queue(settings: Settings, remaining: list[dict]) -> None:
    if remaining:
        atomic_write_json(settings.clean_metadata_path, remaining)
    elif remove_if_exists(settings.clean_metadata_path):
        logger.info("CLEANUP: All items categorized. '%s' deleted.", settings.clean_metadata_path.name)


def run_categorize(
    settings: Settings,
    runtime: LlmRuntime | None = None,
    *,
    sleep: Callable[[float], None] = time.sleep,
    rng: Callable[[float, float], float] = random.uniform,
) -> CategorizeResult:
    result = CategorizeResult()
    queue_path = settings.clean_metadata_path
    if not queue_path.exists():
        logger.info("Queue empty: '%s' not found. Skipping categorization.", queue_path.name)
        return result

    remaining = read_json_list(queue_path)

    _, csv_rows = read_csv_rows(settings.organized_csv_path)
    csv_urls = {normalize_url(row.get("URL", "")) for row in csv_rows} - {""}
    if csv_urls:
        before = len(remaining)
        remaining = [v for v in remaining if normalize_url(v.get("url", "")) not in csv_urls]
        result.reconciled = before - len(remaining)
        if result.reconciled:
            logger.warning("Reconciled %d item(s) already in the CSV from an interrupted run.", result.reconciled)

    if not remaining:
        _save_queue(settings, remaining)
        return result
    if result.reconciled:
        _save_queue(settings, remaining)

    runtime = runtime or build_runtime(settings)
    batch_size = settings.gemini.batch_size
    logger.info(
        "Found %d video(s) to categorize (~%d request(s)) via %s.",
        len(remaining),
        -(-len(remaining) // batch_size),
        runtime.adapter.provider_label,
    )

    while remaining:
        batch = remaining[:batch_size]
        try:
            rows = classify_batch(runtime, batch, settings, sleep=sleep, rng=rng)
        except DailyQuotaExhaustedError as exc:
            result.stopped_reason = str(exc)
        except RateLimitExhaustedError as exc:
            result.stopped_reason = f"{exc}; stopping to preserve API limits."
        except (ValidationError, EmptyResponseError) as exc:
            result.stopped_reason = f"Unparseable LLM response: {exc}"
        except Exception as exc:
            if not result.stopped_reason:
                result.stopped_reason = f"API error: {exc}"
        finally:
            result.requests += 1
        if result.stopped_reason:
            logger.error("Batch failed (%s). Queue left intact for the next run.", result.stopped_reason)
            break

        append_csv_rows(settings.organized_csv_path, CSV_FIELDS, rows)
        remaining = remaining[len(batch):]
        _save_queue(settings, remaining)
        result.categorized += len(batch)
        logger.info("  Categorized batch of %d (%d left).", len(batch), len(remaining))
        if remaining:
            sleep(settings.gemini.pause_between_batches_seconds)

    result.remaining = len(remaining)
    return result
