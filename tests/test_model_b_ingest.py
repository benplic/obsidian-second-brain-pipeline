"""Model B travel routing. No network and no yt-dlp."""

from __future__ import annotations

import inspect
import json
from dataclasses import replace

import pytest
from geopy.exc import GeocoderTimedOut
from pydantic import ValidationError

from second_brain.config import ConfigError, settings_from_dict
from second_brain.dispatch import run_dispatch
from second_brain.model_b.geocode import GeocodeHit, geocode_place, safe_geocode
from second_brain.model_b.ingest import TRAVEL_KEYFRAME_FRACTIONS, caption_is_sparse
from second_brain.model_b.schema import TravelExtraction, place_query, route_travel
from second_brain.model_c.ingest import KEYFRAME_FRACTIONS, extract_keyframes
from second_brain.vault import parse_frontmatter, sanitize_filename

from conftest import FakeRuntime, make_card, rate_limit_error
from test_dispatch import _Jev, _meta, _queue


def _travel(**overrides) -> str:
    payload = {
        "is_travel_related": True,
        "place_name": "Shibuya",
        "country": "Japan",
        "summary": "Late-night ramen counters down a quiet alley",
        "confidence": 0.86,
    }
    payload.update(overrides)
    return json.dumps(payload)


def _hit() -> GeocodeHit:
    return GeocodeHit(latitude=35.6595, longitude=139.7004, address="Shibuya, Tokyo, Japan")


def _no_media(monkeypatch):
    """Keep tests off yt-dlp. Audio and frames are recorded, not downloaded."""
    calls = {"frames": [], "audio": 0}

    def frames(url, tmp, fractions=KEYFRAME_FRACTIONS):
        calls["frames"].append(fractions)
        return ["frame"]

    def audio(*_args, **_kwargs):
        calls["audio"] += 1
        return None

    monkeypatch.setattr("second_brain.model_b.ingest.extract_keyframes", frames)
    monkeypatch.setattr("second_brain.model_b.ingest.extract_audio_transcript", audio)
    return calls


def test_route_travel_thresholds():
    mapped = TravelExtraction.model_validate_json(_travel())
    assert route_travel(mapped, geocoded=True, threshold=0.50) == "mapped"
    assert route_travel(mapped, geocoded=False, threshold=0.50) == "unmapped-travel"
    low = TravelExtraction.model_validate_json(_travel(confidence=0.49))
    assert route_travel(low, geocoded=True, threshold=0.50) == "manual-review"
    other = TravelExtraction.model_validate_json(_travel(is_travel_related=False, confidence=0.99))
    assert route_travel(other, geocoded=True, threshold=0.50) == "manual-review"
    assert place_query(mapped) == "Shibuya, Japan"
    assert place_query(TravelExtraction.model_validate_json(_travel(place_name="  "))) is None


def test_confidence_is_bounded():
    with pytest.raises(ValidationError):
        TravelExtraction.model_validate_json(_travel(confidence=1.2))


def test_model_b_config_rejects_a_bad_threshold(tmp_path):
    with pytest.raises(ConfigError, match="model_b.confidence_threshold"):
        settings_from_dict(
            {"vault_path": str(tmp_path), "model_b": {"confidence_threshold": 1.5}},
            base_dir=tmp_path,
        )


def test_keyframe_default_stays_model_c():
    assert KEYFRAME_FRACTIONS == (0.25, 0.50, 0.75)
    assert TRAVEL_KEYFRAME_FRACTIONS == (0.25, 0.55, 0.85)
    default = inspect.signature(extract_keyframes).parameters["fractions"].default
    assert default == KEYFRAME_FRACTIONS
    assert caption_is_sparse("short", 40)
    assert not caption_is_sparse("x" * 40, 40)


def test_mapped_card_uses_coordinates_not_the_place_as_a_path(settings, monkeypatch):
    _no_media(monkeypatch)
    _queue(settings, "https://example.com/shibuya")
    queries = []
    place = "../../not-a-folder"

    def geocode(query):
        queries.append(query)
        return _hit()

    result = run_dispatch(
        settings,
        FakeRuntime([_travel(place_name=place)]),
        acquire=lambda url: _meta(title="Shibuya?*:ramen", description="x" * 40),
        classifier=_Jev("Travel", 0.95),
        geocode=geocode,
    )
    assert result.created == 1 and result.stopped_reason is None
    assert queries == ["../../not-a-folder, Japan"]
    travel = settings.resources_dir / "Travel"
    card = next(path for path in travel.glob("*.md") if "Hub" not in path.name and "Map" not in path.name)
    assert card.parent.resolve() == travel.resolve()
    assert card.name == f"{sanitize_filename('Shibuya?*:ramen', max_len=50)}.md"
    assert "not-a-folder" not in card.name
    assert not (settings.vault_path / "not-a-folder").exists()
    text = card.read_text(encoding="utf-8")
    fm = parse_frontmatter(text)
    assert fm["status"] == "inbox"
    assert fm["stage"] == "mapped"
    assert fm["destination"] == place
    assert fm["location_name"] == "../../not-a-folder, Japan"
    assert fm["address"] == "Shibuya, Tokyo, Japan"
    assert fm["location"][0] == pytest.approx(35.6595)
    assert fm["location"][1] == pytest.approx(139.7004)
    assert fm["weight"] == 0
    assert fm["mapMarkerColor"] == "#9E9E9E"
    assert fm["tags"] == ["saved-media", "category/travel", "travel/mapped"]
    assert "location: [" in text


def test_unmapped_when_geocode_times_out(settings, monkeypatch):
    _no_media(monkeypatch)
    _queue(settings, "https://example.com/shibuya")

    def geocode(_query):
        raise GeocoderTimedOut("timed out")

    run_dispatch(
        settings,
        FakeRuntime([_travel()]),
        acquire=lambda url: _meta(title="Ramen alley", description="x" * 40),
        classifier=_Jev("Travel", 0.95),
        geocode=geocode,
    )
    card = next(path for path in (settings.resources_dir / "Travel").glob("*.md") if "Hub" not in path.name)
    fm = parse_frontmatter(card.read_text(encoding="utf-8"))
    assert fm["stage"] == "unmapped-travel"
    assert "location" not in fm
    assert "address" not in fm
    assert fm["location_name"] == "Shibuya, Japan"
    assert "travel/unmapped" in fm["tags"]


def test_missing_place_is_unmapped_without_a_geocode_call(settings, monkeypatch):
    calls = _no_media(monkeypatch)
    _queue(settings, "https://example.com/shibuya")

    def geocode(_query):
        raise AssertionError("no place name, so Nominatim must not be called")

    run_dispatch(
        settings,
        FakeRuntime([_travel(place_name=None, country=None)]),
        acquire=lambda url: _meta(title="Silent alley", description="x" * 40),
        classifier=_Jev("Travel", 0.95),
        geocode=geocode,
    )
    # Long caption, no place: audio runs once and returns nothing, so one model call.
    assert calls["frames"] == []
    assert calls["audio"] == 1
    card = next(path for path in (settings.resources_dir / "Travel").glob("*.md") if "Hub" not in path.name)
    fm = parse_frontmatter(card.read_text(encoding="utf-8"))
    assert fm["stage"] == "unmapped-travel"
    assert "location" not in fm
    assert "destination" not in fm


def test_low_confidence_and_non_travel_go_to_manual_review(settings, monkeypatch):
    _no_media(monkeypatch)
    _queue(settings, "https://example.com/low", "https://example.com/other")
    geos = {"n": 0}

    def geocode(_query):
        geos["n"] += 1
        return _hit()

    result = run_dispatch(
        settings,
        FakeRuntime([
            _travel(confidence=0.49),
            _travel(is_travel_related=False, confidence=0.91, place_name=None),
        ]),
        acquire=lambda url: _meta(title="Clip", description="x" * 40),
        classifier=_Jev("Travel", 0.95),
        geocode=geocode,
    )
    assert result.created == 2
    assert geos["n"] == 0
    review = list((settings.resources_dir / "Inbox" / "Manual Review").glob("*.md"))
    assert len(review) == 2
    stages = {parse_frontmatter(path.read_text(encoding="utf-8"))["stage"] for path in review}
    assert stages == {"manual-review"}
    for path in review:
        fm = parse_frontmatter(path.read_text(encoding="utf-8"))
        assert fm["status"] == "inbox"
        assert "location" not in fm
        assert "address" not in fm
        assert "weight" not in fm
        assert "travel/manual-review" in fm["tags"]
        assert path.parent.name == "Manual Review"


def test_sparse_caption_requests_travel_keyframes(settings, monkeypatch):
    calls = _no_media(monkeypatch)
    _queue(settings, "https://example.com/sparse")
    run_dispatch(
        settings,
        FakeRuntime([_travel()]),
        acquire=lambda url: _meta(title="Alley", description="ramen"),
        classifier=_Jev("Travel", 0.95),
        geocode=lambda _query: _hit(),
    )
    assert calls["frames"] == [TRAVEL_KEYFRAME_FRACTIONS]
    assert calls["audio"] == 0


def test_sparse_caption_passes_frames_then_audio(settings, monkeypatch):
    calls = _no_media(monkeypatch)

    def audio(*_args, **_kwargs):
        calls["audio"] += 1
        return "The ramen counter is in Shibuya."

    monkeypatch.setattr("second_brain.model_b.ingest.extract_audio_transcript", audio)
    _queue(settings, "https://example.com/sparse")
    runtime = FakeRuntime([
        _travel(place_name=None, country=None, summary="Silent clip with on-screen text"),
        _travel(),
    ])
    run_dispatch(
        settings,
        runtime,
        acquire=lambda url: _meta(title="Alley", description=""),
        classifier=_Jev("Travel", 0.95),
        geocode=lambda query: _hit() if query == "Shibuya, Japan" else None,
    )
    assert calls["frames"] == [TRAVEL_KEYFRAME_FRACTIONS]
    assert calls["audio"] == 1
    assert runtime.calls[0].extra_image_objects == ["frame"]
    assert "Shibuya" in runtime.calls[1].prompt
    card = next(path for path in (settings.resources_dir / "Travel").glob("*.md") if "Hub" not in path.name)
    assert parse_frontmatter(card.read_text(encoding="utf-8"))["stage"] == "mapped"


def test_long_caption_does_not_download(settings, monkeypatch):
    calls = _no_media(monkeypatch)
    _queue(settings, "https://example.com/long")
    run_dispatch(
        settings,
        FakeRuntime([_travel()]),
        acquire=lambda url: _meta(title="Alley", description="x" * 40),
        classifier=_Jev("Travel", 0.95),
        geocode=lambda _query: _hit(),
    )
    assert calls["frames"] == []
    assert calls["audio"] == 0


def test_geocoder_none_skips_nominatim(settings, monkeypatch):
    _no_media(monkeypatch)
    _queue(settings, "https://example.com/shibuya")
    quiet = replace(settings, crusher=replace(settings.crusher, geocoder="none"))

    def geocode(_query):
        raise AssertionError("geocoder none must not call Nominatim")

    run_dispatch(
        quiet,
        FakeRuntime([_travel()]),
        acquire=lambda url: _meta(title="Alley", description="x" * 40),
        classifier=_Jev("Travel", 0.95),
        geocode=geocode,
    )
    card = next(path for path in (quiet.resources_dir / "Travel").glob("*.md") if "Hub" not in path.name)
    assert parse_frontmatter(card.read_text(encoding="utf-8"))["stage"] == "unmapped-travel"


def test_known_travel_url_is_not_fetched(settings):
    make_card(settings.resources_dir, "Travel", "Already", "https://example.com/known")
    _queue(settings, "https://example.com/known")
    calls = {"n": 0}

    def acquire(url):
        calls["n"] += 1
        return _meta()

    result = run_dispatch(settings, FakeRuntime([]), acquire=acquire, classifier=_Jev("Travel", 0.95))
    assert result.skipped_known == 1 and result.created == 0
    assert calls["n"] == 0


def test_travel_rate_limit_stays_queued(settings, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda _seconds: None)
    _no_media(monkeypatch)
    inbox = _queue(settings, "https://example.com/later")
    result = run_dispatch(
        settings,
        FakeRuntime([rate_limit_error() for _ in range(5)]),
        acquire=lambda url: _meta(description="x" * 40),
        classifier=_Jev("Travel", 0.95),
        geocode=lambda _query: _hit(),
    )
    assert result.created == 0 and result.stopped_reason
    assert "https://example.com/later" in inbox.read_text(encoding="utf-8")


def test_unparseable_travel_response_is_manual_review(settings, monkeypatch):
    _no_media(monkeypatch)
    inbox = _queue(settings, "https://example.com/bad")
    run_dispatch(
        settings,
        FakeRuntime(["this is not json"]),
        acquire=lambda url: _meta(description="x" * 40),
        classifier=_Jev("Travel", 0.95),
    )
    assert inbox.read_text(encoding="utf-8") == ""
    card = next((settings.resources_dir / "Inbox" / "Manual Review").glob("*.md"))
    fm = parse_frontmatter(card.read_text(encoding="utf-8"))
    assert fm["stage"] == "manual-review"
    assert fm["confidence"] == 0.0


def test_geocode_place_skips_when_disabled(settings, monkeypatch):
    monkeypatch.setattr(
        "second_brain.model_b.geocode._limiter",
        lambda _ua: (_ for _ in ()).throw(AssertionError("should not build a limiter")),
    )
    quiet = replace(settings, crusher=replace(settings.crusher, geocoder="none"))
    assert geocode_place("Shibuya, Japan", quiet) is None


def test_geocode_place_reads_a_hit(settings, monkeypatch):
    class _Location:
        latitude = 35.6595
        longitude = 139.7004
        address = "Shibuya, Tokyo, Japan"

    seen = {}

    def geocode(query, **kwargs):
        seen["query"] = query
        seen["kwargs"] = kwargs
        return _Location()

    monkeypatch.setattr("second_brain.model_b.geocode._limiter", lambda _ua: geocode)
    hit = geocode_place("Shibuya, Japan", settings)
    assert hit == _hit()
    assert seen["query"] == "Shibuya, Japan"
    assert seen["kwargs"]["addressdetails"] is True
    assert safe_geocode(lambda _query: (_ for _ in ()).throw(GeocoderTimedOut("slow")), "Shibuya") is None


def test_filename_strips_illegal_characters():
    assert sanitize_filename('A\\B/C*D?E:F"G<H>I|J', max_len=50) == "ABCDEFGHIJ"
    assert sanitize_filename("..", max_len=50) == "Saved Video"
