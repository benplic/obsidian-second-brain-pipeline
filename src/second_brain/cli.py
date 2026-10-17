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
    from .dispatch import run_dispatch

    result = run_dispatch(settings)
    if result.stopped_reason:
        logger.warning("Run stopped early: %s", result.stopped_reason)
    else:
        logger.info("Pipeline completed. Created %d card(s), skipped %d known.", result.created, result.skipped_known)
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
    from .dispatch import run_dispatch

    result = run_dispatch(settings)
    if result.stopped_reason:
        logger.warning("Ingest stopped early: %s", result.stopped_reason)
    return 0


def cmd_model_a_enrich(settings: Settings, args) -> int:
    from .model_a.enrich import run_enrich

    run_enrich(settings)
    return 0


def cmd_model_c_ingest(settings: Settings, args) -> int:
    from .dispatch import run_dispatch

    result = run_dispatch(settings)
    if result.stopped_reason:
        logger.warning("Model C ingest stopped early: %s", result.stopped_reason)
    return 0


def cmd_model_b_ingest(settings: Settings, args) -> int:
    from .dispatch import run_dispatch

    result = run_dispatch(settings)
    if result.stopped_reason:
        logger.warning("Model B ingest stopped early: %s", result.stopped_reason)
    return 0


def cmd_migrate_cards(settings: Settings, args) -> int:
    from .migrate_cards import run_migrate_cards

    run_migrate_cards(settings, apply=args.apply)
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
        max_spend=args.max_spend,
    )
    result = run_crush(settings, opts)
    if result.stopped_reason:
        logger.warning("Crush stopped early: %s", result.stopped_reason)
    return 0


def cmd_setup(_settings: Settings, args) -> int:
    from .setup_ai import run_setup

    return run_setup(args.config)


def cmd_doctor(_settings: Settings, args) -> int:
    from .setup_ai import run_doctor

    return run_doctor(args.config)


def cmd_model_d(settings: Settings, args) -> int:
    if args.repair and not args.scaffold:
        logger.error("--repair requires --scaffold.")
        return 2
    settings.require_vault()
    if args.scaffold:
        from .model_d.hubs import scaffold_hubs

        result = scaffold_hubs(
            settings.resources_dir,
            repair=args.repair,
            resources_subdir=settings.resources_subdir,
        )
        logger.info(
            "Model D hubs: %d created, %d repaired, %d left unchanged.",
            len(result.created),
            len(result.repaired),
            len(result.unchanged),
        )
        return 0
    from .model_d.triage import run_triage

    return run_triage(settings)


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
    sub.add_parser(
        "run",
        help="Enrich pending_links.txt, classify, and write cards (pipeline lock)",
    ).set_defaults(func=cmd_run)
    sub.add_parser("ledger-sync", help="Record vault URLs and tossed cards in the URL ledger").set_defaults(func=cmd_ledger_sync)
    p_travel = sub.add_parser(
        "migrate-travel",
        help="Backfill Travel Map View frontmatter; ensure Hub/Map notes; replace CARTO tiles with OpenStreetMap",
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
    p_cards = sub.add_parser(
        "migrate-cards",
        help="Dry-run: move legacy status words to stage and legalize category tags",
    )
    p_cards.add_argument("--apply", action="store_true", help="Write changes (default is dry-run)")
    p_cards.set_defaults(func=cmd_migrate_cards)
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
    p_crush.add_argument(
        "--max-spend",
        type=float,
        help="Stop this run before Gemini spend exceeds N USD (overrides crusher.max_spend_usd)",
    )
    p_crush.set_defaults(func=cmd_crush)
    sub.add_parser("model-a-ingest", help="Experimental: per-URL tiered analysis (1-3 requests per URL)").set_defaults(func=cmd_model_a_ingest)
    sub.add_parser("model-a-enrich", help="Experimental: re-classify existing high-value cards").set_defaults(func=cmd_model_a_enrich)
    sub.add_parser(
        "model-c-ingest",
        help="Music, Fitness, and Recipes: tiered analysis into actionable notes",
    ).set_defaults(func=cmd_model_c_ingest)
    sub.add_parser(
        "model-b-ingest",
        help="Travel: keyframes, audio fallback, and Nominatim pins",
    ).set_defaults(func=cmd_model_b_ingest)
    p_model_d = sub.add_parser(
        "model-d",
        help="Universal category hubs (--scaffold) or interactive inbox triage (--triage)",
    )
    model_d_mode = p_model_d.add_mutually_exclusive_group(required=True)
    model_d_mode.add_argument("--scaffold", action="store_true", help="Create missing category hubs")
    model_d_mode.add_argument("--triage", action="store_true", help="Triage inbox and manual-review notes")
    p_model_d.add_argument(
        "--repair",
        action="store_true",
        help="With --scaffold, refresh stock Model D hubs that still match a previous template",
    )
    p_model_d.set_defaults(func=cmd_model_d)
    sub.add_parser("setup", help="Interactive wizard: choose LLM provider and write llm: in config.yaml").set_defaults(func=cmd_setup)
    sub.add_parser("doctor", help="Verify API key, text call, and vision probe for configured LLM").set_defaults(func=cmd_doctor)
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
