"""Manual review is the dead-end path, about 1% of a normal batch."""

from second_brain.crusher.review import classify_terminal
from second_brain.crusher.state import (
    STATUS_DONE,
    STATUS_FAILED_RETRYABLE,
    STATUS_MANUAL_REVIEW,
    STATUS_NEEDS_REVIEW,
)


def test_single_failure_is_retried_not_manual():
    assert (
        classify_terminal(failed=True, attempts=1, max_attempts=3, complete=False, flagged=False)
        == STATUS_FAILED_RETRYABLE
    )


def test_unrecoverable_failure_is_manual_review():
    assert (
        classify_terminal(failed=True, attempts=3, max_attempts=3, complete=False, flagged=False)
        == STATUS_MANUAL_REVIEW
    )


def test_incomplete_analysis_is_needs_review_not_manual():
    assert (
        classify_terminal(failed=False, attempts=2, max_attempts=3, complete=False, flagged=False)
        == STATUS_NEEDS_REVIEW
    )


def test_manual_review_rate_is_about_one_percent():
    """A 100-item batch in the shape the crusher is built for.

    90 finish, 8 are usable but incomplete, 1 transient blip is retried,
    1 never produces an analysis. Only that last one is manual review.
    """
    statuses: list[str] = []
    statuses += [
        classify_terminal(failed=False, attempts=1, max_attempts=3, complete=True, flagged=False)
        for _ in range(90)
    ]
    statuses += [
        classify_terminal(failed=False, attempts=1, max_attempts=3, complete=False, flagged=False)
        for _ in range(8)
    ]
    statuses.append(
        classify_terminal(failed=True, attempts=1, max_attempts=3, complete=False, flagged=False)
    )
    statuses.append(
        classify_terminal(failed=True, attempts=3, max_attempts=3, complete=False, flagged=False)
    )
    assert len(statuses) == 100
    assert statuses.count(STATUS_MANUAL_REVIEW) == 1
    assert statuses.count(STATUS_DONE) == 90
    assert statuses.count(STATUS_NEEDS_REVIEW) == 8
    assert statuses.count(STATUS_FAILED_RETRYABLE) == 1
