"""Background scheduler for periodic maintenance runs."""

import logging
import threading
import time
from datetime import datetime, timedelta

log = logging.getLogger(__name__)
_thread = None


def start_scheduler(app):
    """Start background maintenance scheduler."""
    global _thread
    if _thread is not None:
        return

    from mediacleaner.config import get_config
    cfg = get_config()
    schedule_time = cfg.get("maintenance", {}).get("schedule", "03:00")

    _thread = threading.Thread(target=_run_loop, args=(app, schedule_time), daemon=True)
    _thread.start()
    log.info(f"Scheduler started, maintenance runs daily at {schedule_time}")


def _run_loop(app, schedule_time):
    while True:
        now = datetime.now()
        hour, minute = (int(x) for x in schedule_time.split(":"))
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)

        wait_seconds = (target - now).total_seconds()
        log.info(f"Next maintenance in {wait_seconds/3600:.1f}h at {target}")
        time.sleep(wait_seconds)

        with app.app_context():
            _run_maintenance()


def _run_maintenance():
    from mediacleaner.config import get_config
    from mediacleaner.engine import (
        execute_deletions, process_pending_actions, run_evaluation, sync_managed_media,
    )
    from mediacleaner import notify

    cfg = get_config()
    dry_run = cfg.get("maintenance", {}).get("dry_run", True)

    log.info(f"Scheduled maintenance starting (dry_run={dry_run})")
    try:
        sync_managed_media()
        report = run_evaluation(dry_run=dry_run)
        process_pending_actions()
        if not dry_run:
            execute_deletions(report)

        deletions = [r for r in report.results if r.action == "delete"]
        summary = f"Maintenance complete: {len(deletions)} deletions"
        if dry_run:
            summary += " (dry run)"
        log.info(summary)
        notify.send("MediaCleaner Maintenance", summary)
    except Exception as e:
        log.error(f"Maintenance failed: {e}")
