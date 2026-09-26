"""Ledger durability, config loading, and a repo hygiene guard."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from second_brain.config import ConfigError, load_settings, settings_from_dict
from second_brain.ledger import EVENT_CARDED, UrlLedger, sync_ledger_from_vault

from conftest import make_card

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_ledger_survives_torn_last_line(tmp_path):
    path = tmp_path / "ledger.jsonl"
    ledger = UrlLedger(path)
    ledger.record("https://t.co/v/1/", EVENT_CARDED)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write('{"url": "https://t.co/v/2", "ev')  # crash mid-append
    reloaded = UrlLedger(path)
    assert "https://t.co/v/1" in reloaded and "https://t.co/v/2" not in reloaded


def test_ledger_record_is_idempotent(tmp_path):
    ledger = UrlLedger(tmp_path / "l.jsonl")
    ledger.record("https://t.co/a", EVENT_CARDED)
    ledger.record("https://t.co/a", EVENT_CARDED)
    assert len((tmp_path / "l.jsonl").read_text().splitlines()) == 1


def test_sync_backfills_existing_vault(settings):
    make_card(settings.resources_dir, "Comedy", "a", "https://t.co/a")
    make_card(settings.resources_dir, "Comedy", "b", "https://t.co/b", status="tossed")
    ledger = UrlLedger(settings.ledger_path)
    _, carded, tossed = sync_ledger_from_vault(ledger, settings.resources_dir)
    assert (carded, tossed) == (2, 1)
    assert sync_ledger_from_vault(ledger, settings.resources_dir)[1:] == (0, 0)


def test_example_config_loads_and_resolves_relative_paths():
    settings = load_settings(REPO_ROOT / "config.example.yaml")
    assert settings.vault_path == (REPO_ROOT / "examples/sample-vault/The Brain").resolve()
    assert settings.gemini.batch_size == 100
    assert settings.clean_metadata_path.name == "clean_metadata.json"


def test_ledger_defaults_to_hidden_folder_in_vault(tmp_path):
    settings = settings_from_dict({"vault_path": "vault"}, base_dir=tmp_path)
    assert settings.ledger_path == (tmp_path / "vault" / ".second-brain" / "url_ledger.jsonl").resolve()
    explicit = settings_from_dict({"vault_path": "vault", "ledger_path": "elsewhere/l.jsonl"}, base_dir=tmp_path)
    assert explicit.ledger_path == (tmp_path / "elsewhere" / "l.jsonl").resolve()


def test_example_config_uses_vault_ledger():
    settings = load_settings(REPO_ROOT / "config.example.yaml")
    assert settings.ledger_path.parent == settings.vault_path / ".second-brain"


def test_ledger_folder_is_invisible_to_vault_scan(settings):
    from second_brain.ledger import EVENT_CARDED
    from second_brain.vault import get_vault_urls

    UrlLedger(settings.ledger_path).record("https://t.co/a", EVENT_CARDED)
    assert settings.ledger_path.is_file()
    assert get_vault_urls(settings.resources_dir) == set()


def test_missing_config_gives_helpful_error(tmp_path):
    with pytest.raises(ConfigError, match="config.example.yaml"):
        load_settings(tmp_path / "config.yaml")


@pytest.mark.parametrize("raw, message", [
    ({}, "vault_path"),
    ({"vault_path": "v", "gemini": {"batch_sise": 5}}, "Unknown key"),
    ({"vault_path": "v", "gemini": {"batch_size": 0}}, "batch_size"),
    ({"vault_path": "v", "taxonomy": {"X": "../escape"}}, "plain folder"),
])
def test_invalid_config_rejected(tmp_path, raw, message):
    with pytest.raises(ConfigError, match=message):
        settings_from_dict(raw, base_dir=tmp_path)


def _tracked_candidate_files():
    skip_dirs = {".git", ".venv", "__pycache__", ".pytest_cache"}
    for path in REPO_ROOT.rglob("*"):
        if path.is_file() and not (set(path.relative_to(REPO_ROOT).parts) & skip_dirs):
            yield path


def test_no_personal_paths_or_keys_in_repo():
    """Guard against re-introducing hardcoded user paths or pasted API keys."""
    forbidden = [
        re.compile(r"[A-Za-z]:[\\/]+Users[\\/]", re.IGNORECASE),
        re.compile(r"AIza[0-9A-Za-z_\-]{35}"),  # Google API key shape
    ]
    offenders = []
    for path in _tracked_candidate_files():
        if path.name in {"config.yaml", ".env"} or path == Path(__file__).resolve():
            continue  # gitignored local files, and this file's own patterns
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        offenders += [f"{path}: {p.pattern}" for p in forbidden if p.search(text)]
    assert offenders == []
