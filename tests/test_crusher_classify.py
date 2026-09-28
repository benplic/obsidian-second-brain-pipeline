"""Pluggable classifier: Jev request/response handling, fallback, merge rule."""

from __future__ import annotations

import io
import json
from urllib.error import HTTPError

import pytest

from second_brain.config import CrusherSettings
from second_brain.crusher.classify import (
    ClassificationResult,
    JevClassifier,
    SummarizerClassifier,
    build_classifier,
    merge_classification,
    parse_answers,
)
from second_brain.crusher.schema import CrusherAnalysis

TAXONOMY = ["Saved Music", "Travel", "Tech & Coding"]

# Shape from https://www.jevtypesafeai.com/how-to-use (response section).
JEV_RESPONSE = {
    "model": "jev-1.13.0",
    "answers": {
        "category": {"type": "choice", "choice": "Travel", "confidence": 0.92, "probabilities": {"Travel": 0.92}},
        "media_shape": {"type": "choice", "choice": "photo_slideshow", "confidence": 0.8},
        "relevance": {"type": "score", "score": 4.0, "confidence": 0.9},
        "tag::travel-guide": {"type": "noul", "noul": 0.93},
        "tag::humor": {"type": "noul", "noul": 0.05},
        "needs_visuals": {"type": "noul", "noul": 0.7},
    },
    "usage": {"input_tokens": 812, "output_tokens": 40},
}


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def opener_returning(*items):
    """Each item: dict (JSON body) or Exception (raised)."""
    queue = list(items)
    requests = []

    def opener(req, timeout=None):
        requests.append(req)
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return FakeResponse(json.dumps(item).encode("utf-8"))

    return opener, requests


def http_error(code: int) -> HTTPError:
    return HTTPError("https://api.typesafe.ai/v1/systemone", code, "err", {}, None)


def _analysis(category="Saved Music") -> CrusherAnalysis:
    return CrusherAnalysis(category=category, confidence=0.7, title="t", summary="s")


@pytest.fixture
def cs():
    return CrusherSettings(tag_vocabulary=("travel-guide", "humor"), jev_min_confidence=0.7)


def test_no_key_selects_fallback(cs, monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    assert isinstance(build_classifier(cs), SummarizerClassifier)


def test_key_selects_jev(cs, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "ts-test")
    assert isinstance(build_classifier(cs), JevClassifier)


def test_classifier_gemini_forced_even_with_key(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "ts-test")
    assert isinstance(build_classifier(CrusherSettings(classifier="gemini")), SummarizerClassifier)


def test_jev_request_shape_and_parse(cs):
    usage: list[int] = []
    opener, requests = opener_returning(JEV_RESPONSE)
    jev = JevClassifier(cs, "ts-test", on_usage=usage.append, opener=opener, sleep=lambda s: None)
    result = jev.classify("state text", taxonomy=TAXONOMY)

    body = json.loads(requests[0].data.decode("utf-8"))
    assert body["model"] == "jev-1.13.0"  # pinned, not jev-latest
    assert body["questions"]["category"]["type"] == "choice"
    assert set(body["questions"]["category"]["criteria"]) == set(TAXONOMY)
    assert body["questions"]["relevance"]["type"] == "score"
    assert isinstance(body["questions"]["relevance"]["criteria"], list)
    assert body["questions"]["tag::humor"]["type"] == "noul"
    assert requests[0].get_header("Authorization") == "Bearer ts-test"

    assert result.category == "Travel" and result.category_confidence == pytest.approx(0.92)
    assert result.media_shape == "photo_slideshow"
    assert result.relevance == 4.0
    assert result.tags == ["travel-guide"]
    assert usage == [812]


def test_jev_auth_failure_disables_and_falls_back(cs):
    opener, requests = opener_returning(http_error(401))
    jev = JevClassifier(cs, "bad", opener=opener, sleep=lambda s: None)
    assert jev.classify("s", taxonomy=TAXONOMY) is None
    assert jev.disabled
    assert jev.classify("s", taxonomy=TAXONOMY) is None
    assert len(requests) == 1  # no further calls after auth failure


def test_jev_retries_5xx_then_succeeds(cs):
    sleeps: list[float] = []
    opener, requests = opener_returning(http_error(503), JEV_RESPONSE)
    jev = JevClassifier(cs, "k", opener=opener, sleep=sleeps.append)
    assert jev.classify("s", taxonomy=TAXONOMY).category == "Travel"
    assert len(requests) == 2 and sleeps == [2.0]


def test_jev_bad_request_returns_none(cs):
    opener, _ = opener_returning(http_error(422))
    assert JevClassifier(cs, "k", opener=opener, sleep=lambda s: None).classify("s", taxonomy=TAXONOMY) is None


def test_jev_state_truncated(cs):
    opener, requests = opener_returning({"answers": {"needs_visuals": {"type": "noul", "noul": 0.4}}})
    small = CrusherSettings(jev_max_state_chars=1000)
    prob = JevClassifier(small, "k", opener=opener, sleep=lambda s: None).needs_visuals("x" * 5000)
    body = json.loads(requests[0].data.decode("utf-8"))
    assert len(body["state"]) < 1100 and prob == pytest.approx(0.4)


def test_parse_answers_tolerates_missing_keys(cs):
    result = parse_answers({}, cs)
    assert result.category is None and result.tags == [] and result.relevance is None


def test_merge_high_confidence_overrides(cs):
    analysis = _analysis("Saved Music")
    flagged = merge_classification(analysis, parse_answers(JEV_RESPONSE["answers"], cs), cs, TAXONOMY)
    assert not flagged
    assert analysis.category == "Travel"
    assert "Saved Music" in analysis.secondary_categories
    assert analysis.content_tags == ["travel-guide"] and analysis.relevance == 4.0


def test_merge_low_confidence_disagreement_keeps_gemini_and_flags(cs):
    analysis = _analysis("Saved Music")
    result = ClassificationResult(category="Travel", category_confidence=0.4, source="jev")
    assert merge_classification(analysis, result, cs, TAXONOMY) is True
    assert analysis.category == "Saved Music"


def test_merge_ignores_category_outside_taxonomy(cs):
    analysis = _analysis("Saved Music")
    result = ClassificationResult(category="Made Up", category_confidence=0.99, source="jev")
    assert merge_classification(analysis, result, cs, TAXONOMY) is False
    assert analysis.category == "Saved Music"


def test_merge_none_is_noop(cs):
    analysis = _analysis()
    assert merge_classification(analysis, None, cs, TAXONOMY) is False
    assert analysis.content_tags == []
