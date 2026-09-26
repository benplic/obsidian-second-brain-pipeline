"""Answers "have we already seen this URL?" in one place.

A URL is known if it is:
  * in vault card frontmatter (the master registry),
  * in the persistent ledger (cards that were deleted, merged, or tossed), or
  * in flight: already waiting in clean_metadata.json or organized_tiktoks.csv.
    The in-flight check is what makes a crash between "write downstream queue"
    and "pop upstream queue" harmless: the re-run skips instead of re-billing.
"""

from __future__ import annotations

import logging

from ..config import Settings
from ..io_utils import read_csv_rows, read_json_list
from ..ledger import UrlLedger, sync_ledger_from_vault
from ..urls import normalize_url

logger = logging.getLogger(__name__)


def in_flight_urls(settings: Settings) -> set[str]:
    urls = {normalize_url(item.get("url", "")) for item in read_json_list(settings.clean_metadata_path)}
    _, rows = read_csv_rows(settings.organized_csv_path)
    urls |= {normalize_url(row.get("URL", "")) for row in rows}
    urls.discard("")
    return urls


def known_urls(settings: Settings, ledger: UrlLedger) -> set[str]:
    """Sync the ledger from the vault (one walk) and return every known URL."""
    logger.info("Scanning Obsidian vault for existing cards...")
    vault_urls, _, _ = sync_ledger_from_vault(ledger, settings.resources_dir)
    inflight = in_flight_urls(settings)
    logger.debug("Known URLs: vault=%d ledger=%d in-flight=%d", len(vault_urls), len(ledger), len(inflight))
    return vault_urls | ledger.urls | inflight
