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

    from mediapurge.config import get_config
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
    from mediapurge.config import get_config
    from mediapurge.engine import (
        execute_deletions, process_pending_actions, run_evaluation, sync_managed_media,
    )
    from mediapurge.engine import execute_moves
    from mediapurge import notify

    cfg = get_config()
    dry_run = cfg.get("maintenance", {}).get("dry_run", True)

    log.info(f"Scheduled maintenance starting (dry_run={dry_run})")
    try:
        sync_managed_media()
        report = run_evaluation(dry_run=dry_run)
        process_pending_actions()
        if not dry_run:
            execute_deletions(report)
            execute_moves(report)

        deletions = [r for r in report.results if r.action == "delete"]
        moves = [r for r in report.results if r.action == "move"]
        pending = [r for r in report.results if r.action == "pending_confirm"]
        total_bytes = sum(r.file_size for r in deletions)

        lines = []
        mode = "DRY RUN" if dry_run else "LIVE"
        lines.append(f"MediaPurge Maintenance [{mode}]\n")

        if deletions:
            def _human(b):
                for u in ("B", "KB", "MB", "GB", "TB"):
                    if abs(b) < 1024:
                        return f"{b:.1f} {u}"
                    b /= 1024
                return f"{b:.1f} PB"
            lines.append(f"Deleted ({len(deletions)}) — {_human(total_bytes)} recovered:")
            for r in deletions:
                lines.append(f"  • {r.title}")
        else:
            lines.append("No deletions.")

        if pending:
            lines.append(f"\nNotifications sent ({len(pending)}):")
            for r in pending:
                lines.append(f"  • {r.title}")

        if moves:
            lines.append(f"\nMoved ({len(moves)}):")
            for r in moves:
                lines.append(f"  • {r.title} → {r.move_to}")

        if report.errors:
            lines.append(f"\nErrors ({len(report.errors)}):")
            for e in report.errors:
                lines.append(f"  • {e}")

        summary = "\n".join(lines)
        log.info(summary)

        # Only send email if there's something to report
        if deletions or moves or pending or report.errors:
            notify.send("MediaPurge Maintenance", summary)
    except Exception as e:
        log.error(f"Maintenance failed: {e}")
