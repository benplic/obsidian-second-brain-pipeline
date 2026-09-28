"""Decide when an item is done, incomplete, retryable, or a manual-review dead end.

Manual review is only for an item that still has no analysis after every retry.
A short list, a low-confidence category, or a single 503 stays out of that list,
which is what keeps the manual-review share near 1% on a normal batch.
"""

from __future__ import annotations

from .state import STATUS_DONE, STATUS_FAILED_RETRYABLE, STATUS_MANUAL_REVIEW, STATUS_NEEDS_REVIEW


def classify_terminal(
    *,
    failed: bool,
    attempts: int,
    max_attempts: int,
    complete: bool,
    flagged: bool,
) -> str:
    """Map one item's outcome to a crusher status.

    ``failed`` means there is no analysis (Gemini error, or nothing to send).
    Incomplete-but-usable analyses are ``needs-review``, not manual review.
    """
    if failed:
        if attempts >= max(1, max_attempts):
            return STATUS_MANUAL_REVIEW
        return STATUS_FAILED_RETRYABLE
    if complete and not flagged:
        return STATUS_DONE
    return STATUS_NEEDS_REVIEW
