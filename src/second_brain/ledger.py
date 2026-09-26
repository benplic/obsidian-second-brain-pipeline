"""Persistent URL ledger: every URL that ever became a card (or was tossed).

Why: the vault is the primary registry, but it forgets. When a tossed card is
deleted, or Model A merges a clip into an existing movie note, the URL leaves
frontmatter and the next Share Sheet capture of the same video would be
re-ingested and re-billed against the 20 requests/day Gemini budget.

Format: append-only JSON Lines, one event per line, fsynced on every append.
Append-only keeps writes crash-safe (a torn last line is skipped on load).
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from .urls import normalize_url
from .vault import iter_cards

logger = logging.getLogger(__name__)

EVENT_CARDED = "carded"   # a card was written for this URL
EVENT_TOSSED = "tossed"   # the card had status: tossed (safe to delete it now)
EVENT_MERGED = "merged"   # URL folded into another note's body (Model A movies)


class UrlLedger:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._events: dict[str, set[str]] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        with open(self.path, "r", encoding="utf-8") as handle:
            for line_no, line in enumerate(handle, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                    url = normalize_url(record["url"])
                    event = str(record["event"])
                except (json.JSONDecodeError, KeyError, TypeError) as exc:
                    # Most likely a torn write from a crash; the rest is still valid.
                    logger.warning("Ignoring malformed ledger line %s:%d (%s)", self.path, line_no, exc)
                    continue
                self._events.setdefault(url, set()).add(event)

    def __contains__(self, url: str) -> bool:
        return normalize_url(url) in self._events

    def __len__(self) -> int:
        return len(self._events)

    @property
    def urls(self) -> set[str]:
        return set(self._events)

    def has_event(self, url: str, event: str) -> bool:
        return event in self._events.get(normalize_url(url), set())

    def record(self, url: str, event: str, **extra: str) -> None:
        """Append one event and fsync before returning."""
        url = normalize_url(url)
        if self.has_event(url, event):
            return
        record = {"url": url, "event": event, "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), **extra}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        self._events.setdefault(url, set()).add(event)


def sync_ledger_from_vault(ledger: UrlLedger, resources_dir: Path) -> tuple[set[str], int, int]:
    """Backfill the ledger from vault frontmatter.

    Records every card URL (so a pre-existing vault is protected from day one)
    and every ``status: tossed`` card (so the user can delete it afterwards).

    Returns (vault_urls, newly_carded, newly_tossed). Returning the vault URL set
    lets Step 1 reuse this single vault walk for dedupe.
    """
    vault_urls: set[str] = set()
    carded = tossed = 0
    for card in iter_cards(resources_dir):
        vault_urls.add(card.url)
        if card.url not in ledger:
            ledger.record(card.url, EVENT_CARDED, source="vault-sync")
            carded += 1
        if card.status.lower() == "tossed" and not ledger.has_event(card.url, EVENT_TOSSED):
            ledger.record(card.url, EVENT_TOSSED, source="vault-sync")
            tossed += 1
    if carded or tossed:
        logger.info("Ledger sync: +%d carded, +%d tossed (%d URLs total)", carded, tossed, len(ledger))
    return vault_urls, carded, tossed
