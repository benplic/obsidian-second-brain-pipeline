"""Step 2: 100-item mega-batches, response parsing, pop-on-success."""

from __future__ import annotations

import json

import pytest

from second_brain.io_utils import QueueCorruptError, atomic_write_json, read_csv_rows
from second_brain.steps.categorize import build_prompt, parse_response, run_categorize
from second_brain.taxonomy import DEFAULT_FOLDER_MAP

from conftest import FakeClient, mega_response, rate_limit_error, videos


def _queue(settings, items):
    atomic_write_json(settings.clean_metadata_path, items)


def test_parses_full_100_item_response_in_order():
    batch = videos(100)
    rows = parse_response(mega_response(100), batch)
    assert len(rows) == 100
    assert rows[42] == {"Category": "Tech & Coding", "Summary": "summary 42", "Creator": "creator42",
                        "URL": batch[42]["url"], "Title": "Video 42", "Tags": "a, b"}


def test_out_of_order_and_missing_items_default_to_miscellaneous():
    batch = videos(100)
    payload = json.loads(mega_response(100, skip={7, 99}))
    payload["results"].reverse()
    rows = parse_response(json.dumps(payload), batch)
    assert rows[0]["Summary"] == "summary 0"
    assert rows[7]["Category"] == "Miscellaneous" and rows[7]["Summary"] == "No summary"
    assert rows[99]["Category"] == "Miscellaneous"


def test_prompt_is_compact_and_lists_taxonomy():
    batch = [{"title": "T" * 200, "description": "D" * 200}]
    prompt = build_prompt(batch, list(DEFAULT_FOLDER_MAP))
    assert "Art/Organization/Spaces" in prompt
    assert "Tips" in prompt
    assert "life hacks" in prompt
    payload = json.loads(prompt.split("Input Data:\n", 1)[1])
    assert payload == [{"i": 0, "t": "T" * 70, "c": "D" * 90}]


def test_250_items_use_three_requests_and_delete_queue(settings):
    _queue(settings, videos(250))
    client = FakeClient([mega_response(100), mega_response(100), mega_response(50)])
    result = run_categorize(settings, client, sleep=lambda s: None)
    assert result.categorized == 250 and result.requests == 3
    assert not settings.clean_metadata_path.exists()
    _, rows = read_csv_rows(settings.organized_csv_path)
    assert len(rows) == 250
    sent = json.loads(client.models.calls[0]["contents"].split("Input Data:\n", 1)[1])
    assert len(sent) == 100


def test_failure_pops_only_successful_batches(settings):
    _queue(settings, videos(250))
    bad = __import__("google.genai.errors", fromlist=["x"]).ServerError(500, {"error": {"code": 500, "message": "down", "status": "INTERNAL"}})
    client = FakeClient([mega_response(100), bad])
    result = run_categorize(settings, client, sleep=lambda s: None)
    assert result.categorized == 100 and result.stopped_reason
    remaining = json.loads(settings.clean_metadata_path.read_text(encoding="utf-8"))
    assert [v["url"] for v in remaining] == [v["url"] for v in videos(150, start=100)]


def test_backoff_on_429_then_success(settings):
    _queue(settings, videos(10))
    sleeps: list[float] = []
    client = FakeClient([rate_limit_error(), mega_response(10)])
    result = run_categorize(settings, client, sleep=sleeps.append, rng=lambda a, b: a)
    assert result.categorized == 10
    assert sleeps == [9.0]


def test_rate_limit_exhaustion_keeps_queue(settings):
    _queue(settings, videos(5))
    client = FakeClient([rate_limit_error() for _ in range(5)])
    result = run_categorize(settings, client, sleep=lambda s: None)
    assert result.categorized == 0 and "rate limited" in result.stopped_reason
    assert len(json.loads(settings.clean_metadata_path.read_text(encoding="utf-8"))) == 5
    assert not settings.organized_csv_path.exists()


def test_malformed_json_stops_without_losing_items(settings):
    _queue(settings, videos(3))
    result = run_categorize(settings, FakeClient(["not json"]), sleep=lambda s: None)
    assert result.stopped_reason.startswith("Unparseable")
    assert len(json.loads(settings.clean_metadata_path.read_text(encoding="utf-8"))) == 3


def test_crash_between_csv_append_and_pop_is_reconciled_without_api_call(settings):
    items = videos(120)
    _queue(settings, items)
    # Simulate: batch 1 made it into the CSV, then the process died before the pop.
    from second_brain.io_utils import append_csv_rows
    from second_brain.steps.categorize import CSV_FIELDS
    append_csv_rows(settings.organized_csv_path, CSV_FIELDS, parse_response(mega_response(100), items[:100]))

    client = FakeClient([mega_response(20)])
    result = run_categorize(settings, client, sleep=lambda s: None)
    assert result.reconciled == 100 and result.categorized == 20
    assert len(client.models.calls) == 1
    _, rows = read_csv_rows(settings.organized_csv_path)
    assert len(rows) == 120 and len({r["URL"] for r in rows}) == 120


def test_corrupt_queue_is_not_overwritten(settings):
    settings.clean_metadata_path.parent.mkdir(parents=True, exist_ok=True)
    settings.clean_metadata_path.write_text("[{broken", encoding="utf-8")
    with pytest.raises(QueueCorruptError):
        run_categorize(settings, FakeClient([]), sleep=lambda s: None)
    assert settings.clean_metadata_path.read_text(encoding="utf-8") == "[{broken"


def test_empty_queue_file_is_deleted(settings):
    _queue(settings, [])
    run_categorize(settings, FakeClient([]), sleep=lambda s: None)
    assert not settings.clean_metadata_path.exists()
