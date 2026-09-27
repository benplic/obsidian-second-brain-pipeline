"""Step 1 (yt-dlp) and Step 1-alt (Instagram export): dedupe and queue handling."""

from __future__ import annotations

import json

import pytest

from second_brain.io_utils import atomic_write_json
from second_brain.ledger import EVENT_CARDED, UrlLedger
from second_brain.steps.extract_metadata import build_ytdlp_command, run_extract
from second_brain.steps.parse_instagram import InstagramExportError, parse_saved_posts, run_parse_instagram

from conftest import make_card


def _pending(settings, lines):
    settings.pending_links_path.parent.mkdir(parents=True, exist_ok=True)
    settings.pending_links_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _fake_fetcher(fail: set[str] = frozenset()):
    seen: list[str] = []

    def fetch(url):
        seen.append(url)
        if url in fail:
            return None
        return {"url": url, "title": "t", "description": "d", "tags": [], "creator": "c"}

    return fetch, seen


def test_skips_vault_ledger_and_inflight_urls_before_fetching(settings):
    make_card(settings.resources_dir, "Comedy", "a", "https://t.co/v/vault")
    UrlLedger(settings.ledger_path).record("https://t.co/v/deleted", EVENT_CARDED)
    atomic_write_json(settings.clean_metadata_path, [{"url": "https://t.co/v/queued"}])
    _pending(settings, ["https://t.co/v/vault/", "https://t.co/v/deleted", "https://t.co/v/queued", "https://t.co/v/new"])

    fetch, seen = _fake_fetcher()
    result = run_extract(settings, fetcher=fetch)
    assert seen == ["https://t.co/v/new"]
    assert result.skipped_known == 3 and result.extracted == 1
    queue = json.loads(settings.clean_metadata_path.read_text(encoding="utf-8"))
    assert [q["url"] for q in queue] == ["https://t.co/v/queued", "https://t.co/v/new"]
    # File stays so Google Drive / Shortcut keep a stable path; contents erased.
    assert settings.pending_links_path.exists()
    assert settings.pending_links_path.read_text(encoding="utf-8") == ""


def test_failed_and_invalid_lines_stay_for_review(settings):
    _pending(settings, ["https://t.co/v/ok", "https://t.co/v/dead", "not a url", "-o evil"])
    fetch, seen = _fake_fetcher(fail={"https://t.co/v/dead"})
    run_extract(settings, fetcher=fetch)
    assert "not a url" not in seen and "-o evil" not in seen
    left = settings.pending_links_path.read_text(encoding="utf-8").splitlines()
    assert left == ["https://t.co/v/dead", "not a url", "-o evil"]


def test_links_appended_during_run_are_not_lost(settings):
    _pending(settings, ["https://t.co/v/1"])

    def fetch(url):
        # The phone's Shortcut appends a new link while yt-dlp is running.
        with open(settings.pending_links_path, "a", encoding="utf-8") as handle:
            handle.write("https://t.co/v/late\n")
        return {"url": url, "title": "", "description": "", "tags": [], "creator": "c"}

    run_extract(settings, fetcher=fetch)
    assert settings.pending_links_path.read_text(encoding="utf-8").splitlines() == ["https://t.co/v/late"]


def test_ledger_is_backfilled_from_vault_including_tossed(settings):
    make_card(settings.resources_dir, "Comedy", "t", "https://t.co/v/tossed", status="tossed")
    _pending(settings, ["https://t.co/v/tossed"])
    run_extract(settings, fetcher=_fake_fetcher()[0])
    ledger = UrlLedger(settings.ledger_path)
    assert ledger.has_event("https://t.co/v/tossed", "tossed")


def test_tracking_param_variants_are_deduped_before_fetching(settings):
    make_card(settings.resources_dir, "Comedy", "a", "https://www.tiktok.com/@u/video/1")
    _pending(settings, [
        "https://www.tiktok.com/@u/video/1?is_from_webapp=1&sender_device=pc",
        "https://www.instagram.com/reel/NEW1/?igsh=abc",
        "https://www.instagram.com/reel/NEW1/?utm_source=ig_web_copy_link",
    ])
    fetch, seen = _fake_fetcher()
    result = run_extract(settings, fetcher=fetch)
    assert seen == ["https://www.instagram.com/reel/NEW1"]
    assert result.skipped_known == 1


def test_missing_vault_stops_before_creating_ledger(settings):
    import shutil
    from second_brain.config import ConfigError

    shutil.rmtree(settings.vault_path)
    _pending(settings, ["https://t.co/v/1"])
    with pytest.raises(ConfigError):
        run_extract(settings, fetcher=_fake_fetcher()[0])
    assert not settings.vault_path.exists()


def test_missing_pending_file_is_a_noop(settings):
    assert run_extract(settings, fetcher=_fake_fetcher()[0]).extracted == 0


def test_empty_pending_file_is_cleared_not_deleted(settings):
    _pending(settings, [])
    # _pending writes a trailing newline; treat that as an empty queue.
    settings.pending_links_path.write_text("", encoding="utf-8")
    run_extract(settings, fetcher=_fake_fetcher()[0])
    assert settings.pending_links_path.exists()
    assert settings.pending_links_path.read_text(encoding="utf-8") == ""


def test_ytdlp_command_ends_options_before_url():
    cmd = build_ytdlp_command("https://t.co/v/1", cookies_from_browser="edge")
    assert cmd[-2:] == ["--", "https://t.co/v/1"]
    assert cmd[cmd.index("--cookies-from-browser") + 1] == "edge"


IG_EXPORT = {
    "saved_saved_media": [
        {"label_values": [
            {"label": "URL", "href": "https://www.instagram.com/reel/FAKE1/"},
            {"label": "Caption", "value": "Quick pasta #food #dinner #food " + "x" * 80},
            {"title": "Owner", "dict": [{"dict": [{"label": "Username", "value": "fake.chef"}]}]},
        ]},
        {"label_values": [{"label": "URL", "value": "https://www.instagram.com/reel/FAKE2"}]},
        {"label_values": [{"label": "Caption", "value": "no url, dropped"}]},
    ]
}


def test_parse_saved_posts_shape():
    items = parse_saved_posts(IG_EXPORT)
    assert len(items) == 2
    assert items[0]["creator"] == "fake.chef"
    assert items[0]["tags"] == ["food", "dinner"]
    assert items[0]["title"].endswith("...") and len(items[0]["title"]) == 63
    assert items[1]["title"] == "Instagram Saved Post" and items[1]["creator"] == "Unknown"


def test_parse_instagram_merges_into_queue_and_dedupes(settings, tmp_path):
    export = tmp_path / "saved_posts.json"
    export.write_text(json.dumps(IG_EXPORT), encoding="utf-8")
    make_card(settings.resources_dir, "Recipes & Food", "p", "https://www.instagram.com/reel/FAKE2")
    atomic_write_json(settings.clean_metadata_path, [{"url": "https://t.co/v/existing"}])

    result = run_parse_instagram(settings, export_path=export)
    assert result.queued == 1 and result.skipped_known == 1
    queue = json.loads(settings.clean_metadata_path.read_text(encoding="utf-8"))
    assert [q["url"] for q in queue] == ["https://t.co/v/existing", "https://www.instagram.com/reel/FAKE1"]


def test_parse_instagram_missing_export(settings, tmp_path):
    with pytest.raises(InstagramExportError):
        run_parse_instagram(settings, export_path=tmp_path / "missing.json")
