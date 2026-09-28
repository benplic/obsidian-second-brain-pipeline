"""Pluggable structured classification (category, shape, relevance, tags, needs-visuals).

Two implementations:

- ``JevClassifier``: TypeSafe AI's Jev System One model. It returns typed
  answers with calibrated probabilities and never writes text, so it cannot
  hallucinate a category name that is not in the taxonomy. Costs ~$0.042 per
  1M input tokens with free output (https://www.jevtypesafeai.com/how-to-use).
- ``SummarizerClassifier``: no extra call. It defers to the category and shape
  the Gemini summary already produced. Used when no ``TYPESAFE_API_KEY`` is set.

TODO: JevClassifier is built against the published API docs but has not been
exercised against the live endpoint (early-access waitlist). Validate the
request/response shapes and tune ``jev_min_confidence`` once a key is available.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Callable, Protocol, get_args
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from ..config import CrusherSettings
from .acquire import AcquiredMedia
from .schema import CrusherAnalysis, MediaShape

logger = logging.getLogger(__name__)

MEDIA_SHAPES: tuple[str, ...] = tuple(get_args(MediaShape))
RELEVANCE_LEVELS = [
    "no reusable value (meme, filler, dead link)",
    "low value, mildly interesting",
    "some value, worth a skim",
    "useful, specific actionable info",
    "very useful, reference-worthy",
    "must keep, high-value reference",
]
_NEEDS_VISUALS_INSTRUCTIONS = (
    "Based only on the text below, is the key content of this video (list items, places, "
    "products, song or album names, steps) likely shown ONLY as on-screen text or images, "
    "so that it is missing from the transcript and caption?"
)


class JevError(RuntimeError):
    """Jev call failed in a way the caller should fall back from."""


@dataclass
class ClassificationResult:
    category: str | None = None
    category_confidence: float = 0.0
    media_shape: str | None = None
    media_shape_confidence: float = 0.0
    relevance: float | None = None  # 0..5
    tags: list[str] = field(default_factory=list)
    needs_visuals: float | None = None  # probability 0..1
    source: str = "none"


class Classifier(Protocol):
    name: str

    def needs_visuals(self, state: str) -> float | None: ...

    def classify(self, state: str, *, taxonomy: list[str]) -> ClassificationResult | None: ...


class SummarizerClassifier:
    """Fallback: the Gemini summary's own structured fields are the classification."""

    name = "gemini"

    def needs_visuals(self, state: str) -> float | None:
        return None

    def classify(self, state: str, *, taxonomy: list[str]) -> ClassificationResult | None:
        return None


class JevClassifier:
    """One batched Jev request per call; all questions share the state token cost."""

    name = "jev"

    def __init__(
        self,
        settings: CrusherSettings,
        api_key: str,
        *,
        on_usage: Callable[[int], None] | None = None,
        opener: Callable = urlopen,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.settings = settings
        self._api_key = api_key
        self._on_usage = on_usage
        self._opener = opener
        self._sleep = sleep
        # Flipped on 401/403 so a bad key does not burn 2,000 failing requests.
        self.disabled = False

    # -- transport --------------------------------------------------------

    def _post(self, state: str, questions: dict) -> dict:
        if self.disabled:
            raise JevError("Jev disabled for this run after an auth failure.")
        body = json.dumps(
            {"model": self.settings.jev_model, "state": state, "questions": questions}
        ).encode("utf-8")
        attempts = 3
        for attempt in range(1, attempts + 1):
            req = Request(
                self.settings.jev_endpoint,
                data=body,
                method="POST",
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
            )
            try:
                with self._opener(req, timeout=self.settings.jev_timeout_seconds) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
            except HTTPError as exc:
                if exc.code in (401, 403):
                    self.disabled = True
                    logger.error("Jev rejected the API key (HTTP %s). Falling back to Gemini classification.", exc.code)
                    raise JevError(f"auth failed ({exc.code})") from exc
                if exc.code == 429 or exc.code >= 500:
                    # TODO: honor Retry-After / rate-limit headers once observed on the live API.
                    if attempt < attempts:
                        self._sleep(2.0 * attempt)
                        continue
                raise JevError(f"HTTP {exc.code}") from exc
            except (URLError, TimeoutError, OSError) as exc:
                if attempt < attempts:
                    self._sleep(2.0 * attempt)
                    continue
                raise JevError(f"network: {exc}") from exc
            except json.JSONDecodeError as exc:
                raise JevError("non-JSON response") from exc
            usage = (payload.get("usage") or {}).get("input_tokens")
            if self._on_usage and isinstance(usage, (int, float)):
                self._on_usage(int(usage))
            answers = payload.get("answers")
            if not isinstance(answers, dict):
                raise JevError("response has no 'answers' map")
            return answers
        raise JevError("exhausted retries")  # pragma: no cover

    def _trim(self, state: str) -> str:
        cap = max(1000, int(self.settings.jev_max_state_chars))
        return state if len(state) <= cap else state[:cap] + " [truncated]"

    # -- questions --------------------------------------------------------

    def needs_visuals(self, state: str) -> float | None:
        try:
            answers = self._post(
                self._trim(state), {"needs_visuals": {"type": "noul", "instructions": _NEEDS_VISUALS_INSTRUCTIONS}}
            )
        except JevError as exc:
            logger.warning("Jev needs_visuals failed: %s", exc)
            return None
        return _noul(answers.get("needs_visuals"))

    def build_questions(self, taxonomy: list[str]) -> dict:
        questions: dict = {
            "category": {
                "type": "choice",
                "instructions": "Which library category does this saved short video belong in, judged by its "
                "actual content rather than hashtags?",
                "criteria": {name: name for name in taxonomy},
            },
            "media_shape": {
                "type": "choice",
                "instructions": "What format is this post?",
                "criteria": {shape: shape.replace("_", " ") for shape in MEDIA_SHAPES if shape != "unavailable"},
            },
            "relevance": {
                "type": "score",
                "instructions": "How much reusable reference value does this content have for a personal knowledge base?",
                "criteria": RELEVANCE_LEVELS,
            },
        }
        for tag in self.settings.tag_vocabulary:
            questions[f"tag::{tag}"] = {
                "type": "noul",
                "instructions": f"Does the tag '{tag}' accurately describe this content?",
            }
        return questions

    def classify(self, state: str, *, taxonomy: list[str]) -> ClassificationResult | None:
        try:
            answers = self._post(self._trim(state), self.build_questions(taxonomy))
        except JevError as exc:
            logger.warning("Jev classify failed, using Gemini fields: %s", exc)
            return None
        return parse_answers(answers, self.settings)


def _noul(answer) -> float | None:
    if isinstance(answer, dict) and isinstance(answer.get("noul"), (int, float)):
        return float(answer["noul"])
    return None


def parse_answers(answers: dict, settings: CrusherSettings) -> ClassificationResult:
    """Map a Jev ``answers`` dict to ``ClassificationResult``. Tolerates missing keys."""
    result = ClassificationResult(source="jev")
    cat = answers.get("category") or {}
    if isinstance(cat, dict) and cat.get("choice"):
        result.category = str(cat["choice"])
        result.category_confidence = float(cat.get("confidence") or 0.0)
    shape = answers.get("media_shape") or {}
    if isinstance(shape, dict) and shape.get("choice"):
        result.media_shape = str(shape["choice"])
        result.media_shape_confidence = float(shape.get("confidence") or 0.0)
    rel = answers.get("relevance") or {}
    if isinstance(rel, dict) and isinstance(rel.get("score"), (int, float)):
        result.relevance = float(rel["score"])
    for key, value in answers.items():
        if not key.startswith("tag::"):
            continue
        prob = _noul(value)
        if prob is not None and prob >= settings.jev_tag_threshold:
            result.tags.append(key.split("::", 1)[1])
    result.needs_visuals = _noul(answers.get("needs_visuals"))
    return result


def build_classifier(settings: CrusherSettings, *, on_usage: Callable[[int], None] | None = None) -> Classifier:
    """``auto`` picks Jev when its key env var is set; ``jev`` without a key warns and falls back."""
    key = (os.environ.get(settings.jev_api_key_env_var) or "").strip()
    if settings.classifier == "gemini":
        return SummarizerClassifier()
    if key:
        return JevClassifier(settings, key, on_usage=on_usage)
    if settings.classifier == "jev":
        logger.warning(
            "crusher.classifier=jev but %s is not set; using Gemini summary fields instead.",
            settings.jev_api_key_env_var,
        )
    return SummarizerClassifier()


def build_state(media: AcquiredMedia, analysis: CrusherAnalysis | None = None, *, current_category: str = "") -> str:
    """Compact text state for Jev. Frames are not sent (Jev is text-in only);
    the summary/items describe what the frames showed."""
    parts = [
        f"Title: {media.title}",
        f"Creator: {media.creator}",
        f"Caption: {media.description}",
        f"Hashtags: {', '.join(media.tags[:20])}",
        f"Current folder: {current_category or 'unknown'}",
        f"Transcript ({media.transcript_source}): {media.transcript_text or 'none'}",
    ]
    if analysis is not None:
        parts.append(f"Summary: {analysis.summary}")
        if analysis.findings:
            parts.append("Findings: " + " | ".join(analysis.findings[:15]))
        if analysis.items:
            parts.append("Items: " + "; ".join(i.name for i in analysis.items[:40]))
    return "\n".join(parts)


def merge_classification(
    analysis: CrusherAnalysis,
    result: ClassificationResult | None,
    settings: CrusherSettings,
    taxonomy: list[str],
) -> bool:
    """Apply Jev results onto the Gemini analysis in place. Returns True to flag for review.

    Rule: Jev's category wins only at ``jev_min_confidence`` or above. Below
    that, Gemini's category is kept, and a disagreement is flagged for review
    (agreement at low confidence is not worth a human's time).
    """
    if result is None:
        return False
    analysis.relevance = result.relevance
    analysis.content_tags = list(result.tags)
    flag = False
    if result.category and result.category in taxonomy:
        if result.category_confidence >= settings.jev_min_confidence:
            if result.category != analysis.category:
                logger.info(
                    "Jev overrides category %r -> %r (conf %.2f)",
                    analysis.category,
                    result.category,
                    result.category_confidence,
                )
                if analysis.category and analysis.category not in analysis.secondary_categories:
                    analysis.secondary_categories.append(analysis.category)
                analysis.category = result.category
                analysis.recategorize = True
        elif result.category != analysis.category:
            flag = True
    if (
        result.media_shape in MEDIA_SHAPES
        and result.media_shape_confidence >= settings.jev_min_confidence
    ):
        analysis.media_shape = result.media_shape  # type: ignore[assignment]
    return flag
