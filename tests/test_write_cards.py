"""Step 3: per-card write + pop, crash safety, ledger-based dedupe."""

from __future__ import annotations

import pytest

from second_brain.io_utils import atomic_write_csv, read_csv_rows
from second_brain.ledger import UrlLedger
from second_brain.steps import write_cards as wc
from second_brain.steps.categorize import CSV_FIELDS
from second_brain.vault import get_vault_urls, parse_frontmatter

from conftest import make_card


def _rows(n, start=0, title=None):
    return [{"Category": "Comedy/Funny", "Summary": f"s{i}", "Creator": f"c{i}", "URL": f"https://t.co/v/{i}",
             "Title": title or f"Clip {i}", "Tags": "x"} for i in range(start, start + n)]


def _csv(settings, rows):
    atomic_write_csv(settings.organized_csv_path, CSV_FIELDS, rows)


def _cards(settings):
    return sorted(p for p in settings.resources_dir.rglob("*.md"))


def test_happy_path_writes_cards_records_ledger_and_deletes_csv(settings):
    _csv(settings, _rows(3))
    result = wc.run_write_cards(settings)
    assert result.created == 3
    assert not settings.organized_csv_path.exists()
    assert get_vault_urls(settings.resources_dir) == {f"https://t.co/v/{i}" for i in range(3)}
    assert UrlLedger(settings.ledger_path).urls == {f"https://t.co/v/{i}" for i in range(3)}
    card = parse_frontmatter(_cards(settings)[0].read_text(encoding="utf-8"))
    assert card["status"] == "inbox" and card["tags"][0] == "saved-media"


def test_crash_mid_run_keeps_unwritten_rows_and_rerun_has_no_duplicates(settings, monkeypatch):
    _csv(settings, _rows(5))
    real_write = wc.write_card
    calls = {"n": 0}

    def crashing_write(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 3:
            raise KeyboardInterrupt  # simulate the process being killed mid-card
        return real_write(*args, **kwargs)

    monkeypatch.setattr(wc, "write_card", crashing_write)
    with pytest.raises(KeyboardInterrupt):
        wc.run_write_cards(settings)

    _, left = read_csv_rows(settings.organized_csv_path)
    assert [r["URL"] for r in left] == [f"https://t.co/v/{i}" for i in (2, 3, 4)]
    assert len(_cards(settings)) == 2
    assert not list(settings.resources_dir.rglob("*.tmp"))

    monkeypatch.setattr(wc, "write_card", real_write)
    result = wc.run_write_cards(settings)
    assert result.created == 3
    assert len(_cards(settings)) == 5
    assert not settings.organized_csv_path.exists()


def test_card_written_but_pop_lost_is_skipped_on_rerun(settings):
    rows = _rows(2)
    make_card(settings.resources_dir, "Comedy", "Clip 0", rows[0]["URL"])
    _csv(settings, rows)
    result = wc.run_write_cards(settings)
    assert result.skipped_known == 1 and result.created == 1
    assert len(_cards(settings)) == 2
    assert rows[0]["URL"] in UrlLedger(settings.ledger_path)


def test_deleted_card_is_not_recreated_thanks_to_ledger(settings):
    _csv(settings, _rows(1))
    wc.run_write_cards(settings)
    for card in _cards(settings):
        card.unlink()
    _csv(settings, _rows(1))
    result = wc.run_write_cards(settings)
    assert result.created == 0 and result.skipped_known == 1
    assert _cards(settings) == []


def test_same_title_different_urls_get_unique_files(settings):
    _csv(settings, _rows(3, title="Same Title"))
    wc.run_write_cards(settings)
    names = sorted(p.name for p in _cards(settings))
    assert names == ["Same Title (2).md", "Same Title (3).md", "Same Title.md"]


def test_write_error_keeps_row_for_retry(settings, monkeypatch):
    _csv(settings, _rows(3))
    real_write = wc.write_card

    def flaky(target_dir, stem, content):
        if stem == "Clip 1":
            raise PermissionError("locked by sync client")
        return real_write(target_dir, stem, content)

    monkeypatch.setattr(wc, "write_card", flaky)
    result = wc.run_write_cards(settings)
    assert result.created == 2 and result.failed == 1
    _, left = read_csv_rows(settings.organized_csv_path)
    assert [r["URL"] for r in left] == ["https://t.co/v/1"]


def test_unknown_category_goes_to_inbox(settings):
    rows = _rows(1)
    rows[0]["Category"] = "Made Up Category"
    _csv(settings, rows)
    wc.run_write_cards(settings)
    assert _cards(settings)[0].parent.name == "Inbox"


def test_missing_vault_is_a_config_error(settings, tmp_path):
    import shutil
    from second_brain.config import ConfigError

    shutil.rmtree(settings.vault_path)
    _csv(settings, _rows(1))
    with pytest.raises(ConfigError):
        wc.run_write_cards(settings)
