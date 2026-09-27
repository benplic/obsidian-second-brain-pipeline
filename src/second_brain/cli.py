"""Command-line entry point: ``python -m second_brain <command>`` or ``second-brain <command>``.

Exit codes: 0 = done (including partial progress such as a Gemini batch that
stopped early; the queue is intact), 1 = unexpected failure, 2 = config/setup
problem the user must fix.
"""

from __future__ import annotations

import argparse
import logging
import sys

from .config import ConfigError, Settings, load_settings
from .gemini import MissingApiKeyError
from .io_utils import QueueCorruptError
from .steps.parse_instagram import InstagramExportError

logger = logging.getLogger("second_brain")


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    # Third-party HTTP chatter drowns out pipeline progress at INFO.
    for noisy in ("httpx", "httpcore", "google_genai", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _load_dotenv() -> None:
    """Load a gitignored .env (if present) so GEMINI_API_KEY need not be a system variable."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        logger.debug("python-dotenv not installed; relying on real environment variables")
        return
    load_dotenv(override=False)


def cmd_extract(settings: Settings, args) -> int:
    from .steps.extract_metadata import run_extract

    run_extract(settings)
    return 0


def cmd_parse_instagram(settings: Settings, args) -> int:
    from .steps.parse_instagram import run_parse_instagram

    run_parse_instagram(settings, export_path=args.export)
    return 0


def cmd_categorize(settings: Settings, args) -> int:
    from .steps.categorize import run_categorize

    result = run_categorize(settings)
    if result.stopped_reason:
        logger.warning("Categorization stopped early; %d item(s) remain queued.", result.remaining)
    return 0


def cmd_write_cards(settings: Settings, args) -> int:
    from .steps.write_cards import run_write_cards

    run_write_cards(settings)
    return 0


def cmd_run(settings: Settings, args) -> int:
    for step in (cmd_extract, cmd_categorize, cmd_write_cards):
        code = step(settings, args)
        if code:
            return code
    logger.info("Pipeline completed.")
    return 0


def cmd_ledger_sync(settings: Settings, args) -> int:
    from .ledger import UrlLedger, sync_ledger_from_vault

    settings.require_vault()
    ledger = UrlLedger(settings.ledger_path)
    _, carded, tossed = sync_ledger_from_vault(ledger, settings.resources_dir)
    logger.info("Ledger: %d URL(s) total (+%d carded, +%d tossed). Tossed cards can now be deleted safely.",
                len(ledger), carded, tossed)
    return 0


def cmd_model_a_ingest(settings: Settings, args) -> int:
    from .model_a.ingest import run_ingest

    run_ingest(settings)
    return 0


def cmd_model_a_enrich(settings: Settings, args) -> int:
    from .model_a.enrich import run_enrich

    run_enrich(settings)
    return 0


def cmd_crush(settings: Settings, args) -> int:
    from .crusher import CrushOptions, run_crush

    opts = CrushOptions(
        apply=args.apply,
        limit=args.limit,
        folder=args.folder,
        url=args.url,
        reprocess=args.reprocess,
        write_children=not args.no_children,
    )
    result = run_crush(settings, opts)
    if result.stopped_reason:
        logger.warning("Crush stopped early: %s", result.stopped_reason)
    return 0


def cmd_migrate_travel(settings: Settings, args) -> int:
    from .migrate_travel import run_migrate_travel

    run_migrate_travel(settings, apply=args.apply, force_templates=args.force_templates)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="second-brain", description="Saved videos -> Obsidian cards pipeline.")
    parser.add_argument("-c", "--config", help="Path to config.yaml (default: $SECOND_BRAIN_CONFIG or ./config.yaml)")
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("extract", help="Step 1: pending_links.txt -> clean_metadata.json (yt-dlp)").set_defaults(func=cmd_extract)
    p_ig = sub.add_parser("parse-instagram", help="Step 1 alt: Meta saved_posts.json -> clean_metadata.json")
    p_ig.add_argument("--export", help="Override instagram_export_path")
    p_ig.set_defaults(func=cmd_parse_instagram)
    sub.add_parser("categorize", help="Step 2: clean_metadata.json -> organized_tiktoks.csv (Gemini)").set_defaults(func=cmd_categorize)
    sub.add_parser("write-cards", help="Step 3: organized_tiktoks.csv -> vault cards").set_defaults(func=cmd_write_cards)
    sub.add_parser("run", help="Steps 1-3 in order").set_defaults(func=cmd_run)
    sub.add_parser("ledger-sync", help="Record vault URLs and tossed cards in the URL ledger").set_defaults(func=cmd_ledger_sync)
    p_travel = sub.add_parser(
        "migrate-travel",
        help="Backfill Travel Map View frontmatter on existing cards; ensure Travel Hub/Map notes",
    )
    p_travel.add_argument(
        "--apply",
        action="store_true",
        help="Write changes (default is dry-run)",
    )
    p_travel.add_argument(
        "--force-templates",
        action="store_true",
        help="Overwrite Travel Hub.md / Travel Map.md even if they already exist",
    )
    p_travel.set_defaults(func=cmd_migrate_travel)
    p_crush = sub.add_parser(
        "crush",
        help="Multimodal re-analysis of existing vault cards (default: dry-run + report)",
    )
    p_crush.add_argument("--apply", action="store_true", help="Rewrite vault cards from cached analysis")
    p_crush.add_argument("--limit", type=int, help="Process at most N cards")
    p_crush.add_argument("--folder", help="Only cards under this Resources subfolder (e.g. Travel)")
    p_crush.add_argument("--url", help="Process a single normalized URL")
    p_crush.add_argument("--reprocess", action="store_true", help="Ignore crusher state/cache for this run")
    p_crush.add_argument("--no-children", action="store_true", help="Do not create child item notes")
    p_crush.set_defaults(func=cmd_crush)
    sub.add_parser("model-a-ingest", help="Experimental: per-URL tiered analysis (1-3 requests per URL)").set_defaults(func=cmd_model_a_ingest)
    sub.add_parser("model-a-enrich", help="Experimental: re-classify existing high-value cards").set_defaults(func=cmd_model_a_enrich)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logging(args.verbose)
    _load_dotenv()
    try:
        settings = load_settings(args.config)
        return args.func(settings, args)
    except (ConfigError, MissingApiKeyError, QueueCorruptError, InstagramExportError) as exc:
        logger.error("%s", exc)
        return 2
    except KeyboardInterrupt:
        logger.warning("Interrupted. Queues are saved after every item/batch; re-run to continue.")
        return 130
    except Exception:  # noqa: BLE001 - last-resort handler so the .bat window shows a traceback
        logger.exception("Unexpected failure")
        return 1


if __name__ == "__main__":
    sys.exit(main())
