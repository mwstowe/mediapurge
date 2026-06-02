"""Daily maintenance job — sync, evaluate, delete, notify."""

import argparse
import logging
import sys

from mediacleaner.config import load_config, get_config
from mediacleaner.db import init_db
from mediacleaner.engine import EngineReport, execute_deletions, run_evaluation, sync_managed_media, process_pending_actions, get_confirmed_deletions
from mediacleaner import notify

log = logging.getLogger("mediacleaner")


def _format_report(report: EngineReport, dry_run: bool) -> str:
    lines = []
    mode = "DRY RUN" if dry_run else "LIVE"
    lines.append(f"=== MediaCleaner Maintenance [{mode}] ===\n")

    deletions = [r for r in report.results if r.action == "delete"]
    if deletions:
        lines.append(f"Deletions ({len(deletions)}):")
        for r in deletions:
            lines.append(f"  - {r.title} [{r.manager}] — {r.reason}")
    else:
        lines.append("No deletions.")

    if report.orphans:
        lines.append(f"\nOrphans detected ({len(report.orphans)}):")
        for r in report.orphans:
            lines.append(f"  - {r.title}")

    if report.errors:
        lines.append(f"\nErrors ({len(report.errors)}):")
        for e in report.errors:
            lines.append(f"  - {e}")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="MediaCleaner maintenance job")
    parser.add_argument("--dry-run", action="store_true", default=None)
    parser.add_argument("--config", type=str, default=None)
    args = parser.parse_args()

    load_config(args.config)
    cfg = get_config()
    init_db()

    # Configure logging
    log_file = cfg.get("maintenance", {}).get("log_file")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            *([] if not log_file else [logging.FileHandler(log_file)]),
        ],
    )

    dry_run = args.dry_run if args.dry_run is not None else cfg.get("maintenance", {}).get("dry_run", True)

    log.info("Starting maintenance run (dry_run=%s)", dry_run)

    # Step 1: Sync managed media cache
    log.info("Syncing managed media cache...")
    try:
        sync_managed_media()
    except Exception as e:
        log.error(f"Failed to sync managed media: {e}")
        sys.exit(1)

    # Step 2+3: Evaluate rules (includes orphan detection)
    log.info("Evaluating rules...")
    report = run_evaluation(dry_run=dry_run)

    # Step 4: Process pending confirmations (check for cancellation or expiry)
    log.info("Processing pending confirmations...")
    process_pending_actions()

    # Step 5: Execute deletions if not dry run
    if not dry_run:
        log.info("Executing deletions...")
        execute_deletions(report)

    # Step 5: Report
    summary = _format_report(report, dry_run)
    log.info(summary)

    # Step 6: Notify
    notify.send("MediaCleaner Report", summary)

    log.info("Maintenance complete.")


if __name__ == "__main__":
    main()
