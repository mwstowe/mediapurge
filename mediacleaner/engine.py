"""Rule evaluation engine — resolves rules, evaluates conditions, detects orphans."""

import json
import logging
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from mediacleaner.clients import medusa, plex, radarr, sonarr, ombi
from mediacleaner.db import get_session
from mediacleaner.models import ActionLog, ManagedMedia, PendingAction, Rule
from mediacleaner import notify

log = logging.getLogger(__name__)


@dataclass
class EvalResult:
    title: str
    rating_key: str
    action: str  # keep, delete, orphan_detected
    rule_id: int | None = None
    reason: str = ""
    manager: str = "none"
    manager_id: int | None = None


@dataclass
class EngineReport:
    results: list[EvalResult] = field(default_factory=list)
    orphans: list[EvalResult] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def sync_managed_media():
    """Refresh the managed_media cache from Sonarr, Radarr, and Medusa."""
    session = get_session()
    session.query(ManagedMedia).delete()
    now = datetime.now(timezone.utc)

    for s in sonarr.get_all_series():
        session.add(ManagedMedia(
            title=s["title"], file_path=s["path"],
            manager="sonarr", manager_id=s["id"], last_synced=now,
        ))

    for m in radarr.get_all_movies():
        session.add(ManagedMedia(
            title=m["title"], file_path=m.get("path", ""),
            manager="radarr", manager_id=m["id"], last_synced=now,
        ))

    for s in medusa.get_all_shows():
        path = s.get("config", {}).get("location", s.get("location", ""))
        slug = s.get("id", {}).get("slug", s.get("slug", ""))
        session.add(ManagedMedia(
            title=s.get("title", ""), file_path=path,
            manager="medusa", manager_id=slug, last_synced=now,
        ))

    session.commit()
    session.close()


def find_manager(item) -> tuple[str, int | None]:
    """Determine which application manages a Plex item by matching file paths."""
    paths = plex.get_file_paths(item)
    if not paths:
        return "none", None

    session = get_session()
    managed = session.execute(select(ManagedMedia)).scalars().all()
    session.close()

    for m in managed:
        if not m.file_path:
            continue
        for p in paths:
            if p.startswith(m.file_path):
                return m.manager, m.manager_id
    return "none", None


def resolve_rule(item, library_name: str) -> Rule | None:
    """Find the most specific rule: episode > season > show > library."""
    session = get_session()
    key = str(item.ratingKey)

    # Episode-specific rule
    rule = session.execute(
        select(Rule).where(Rule.scope == "episode", Rule.plex_rating_key == key, Rule.enabled == True)
    ).scalar_one_or_none()

    if rule is None:
        # Season-level rule (match by season rating key)
        season_key = str(getattr(item, "parentRatingKey", ""))
        if season_key:
            rule = session.execute(
                select(Rule).where(Rule.scope == "season", Rule.plex_rating_key == season_key, Rule.enabled == True)
            ).scalar_one_or_none()

    if rule is None:
        # Show/movie-level rule
        show_key = str(getattr(item, "grandparentRatingKey", getattr(item, "ratingKey", "")))
        rule = session.execute(
            select(Rule).where(Rule.scope == "show", Rule.plex_rating_key == show_key, Rule.enabled == True)
        ).scalar_one_or_none()

    if rule is None:
        # Library-level rule
        rule = session.execute(
            select(Rule).where(
                Rule.scope == "library", Rule.plex_library == library_name, Rule.enabled == True
            )
        ).scalar_one_or_none()

    session.close()
    return rule


def evaluate_item(item, rule: Rule) -> tuple[str, str]:
    """Evaluate a single non-show item against a rule. Returns (action, reason)."""
    if rule.action == "keep":
        return "keep", "rule action is keep"

    # Check on-deck protection
    if rule.protect_on_deck and plex.is_on_deck(item):
        return "keep", "protected: on deck"

    # Check watched status
    users = [u.strip() for u in rule.watched_by.split(",")]
    if "any" in users:
        watched, last_viewed = item.isWatched, getattr(item, "lastViewedAt", None)
    else:
        watched, last_viewed = plex.is_watched_by(item, users)

    # Check max_days_age (hard age limit, ignores watch status)
    if rule.max_days_age > 0:
        age = plex.days_since_added(item)
        if age >= rule.max_days_age:
            return "delete", f"exceeded max age ({age} >= {rule.max_days_age} days)"

    # Must be watched for deletion (unless max_days_age triggered above)
    if not watched:
        return "keep", "not yet watched"

    # Check min_days_watched grace period
    days = plex.days_since_watched(last_viewed)
    if days is None:
        return "keep", "no last-viewed date"
    if days < rule.min_days_watched:
        return "keep", f"watched {days}d ago, need {rule.min_days_watched}d"

    return "delete", f"watched {days}d ago by {rule.watched_by}"


def evaluate_show_episodes(show, rule: Rule) -> list[tuple]:
    """Evaluate episodes of a show. Returns list of (item, action, reason).
    May return the show itself as the item if the whole show should be deleted."""
    if rule.action == "keep":
        return [(show, "keep", "rule action is keep")]

    # Check all_watched condition — all episodes must be watched before any action
    if rule.all_watched:
        users = [u.strip() for u in rule.watched_by.split(",")]
        all_done, last_viewed = plex.all_episodes_watched_by(show, users)
        if not all_done:
            return [(show, "keep", "not all episodes watched yet")]
        # All watched — check grace period (min_days_watched applies to when last ep was watched)
        days = plex.days_since_watched(last_viewed)
        if days is not None and days < rule.min_days_watched:
            return [(show, "keep", f"all watched {days}d ago, need {rule.min_days_watched}d")]
        # Eligible for deletion (whole show)
        if rule.confirm_before_delete:
            return [(show, "pending_confirm", f"all watched {days}d ago, awaiting confirmation")]
        return [(show, "delete_show", f"all watched {days}d ago by {rule.watched_by}")]

    # Check show-level inactivity timeout (delete entire show)
    if rule.max_days_inactive > 0:
        inactive_days = plex.days_since_last_activity(show)
        if inactive_days is not None and inactive_days >= rule.max_days_inactive:
            return [(show, "delete_show", f"inactive {inactive_days}d (limit {rule.max_days_inactive}d)")]
        # If never watched and added long ago, also consider inactive
        if inactive_days is None:
            age = plex.days_since_added(show)
            if age >= rule.max_days_inactive:
                return [(show, "delete_show", f"never watched, added {age}d ago (limit {rule.max_days_inactive}d)")]

    episodes = show.episodes()
    # Sort newest first (by season + episode number)
    episodes.sort(key=lambda e: (e.parentIndex or 0, e.index or 0), reverse=True)

    results = []
    users = [u.strip() for u in rule.watched_by.split(",")]

    for i, ep in enumerate(episodes):
        # Always keep the newest min_episodes
        if rule.min_episodes > 0 and i < rule.min_episodes:
            results.append((ep, "keep", f"protected: within latest {rule.min_episodes} episodes"))
            continue

        # Check on-deck
        if rule.protect_on_deck and plex.is_on_deck(ep):
            results.append((ep, "keep", "protected: on deck"))
            continue

        # Check watched
        if "any" in users:
            watched, last_viewed = ep.isWatched, getattr(ep, "lastViewedAt", None)
        else:
            watched, last_viewed = plex.is_watched_by(ep, users)

        # Hard age limit
        if rule.max_days_age > 0:
            age = plex.days_since_added(ep)
            if age >= rule.max_days_age:
                results.append((ep, "delete", f"exceeded max age ({age}d)"))
                continue

        if not watched:
            results.append((ep, "keep", "not yet watched"))
            continue

        days = plex.days_since_watched(last_viewed)
        if days is None:
            results.append((ep, "keep", "no last-viewed date"))
            continue
        if days < rule.min_days_watched:
            results.append((ep, "keep", f"watched {days}d ago, need {rule.min_days_watched}d"))
            continue

        results.append((ep, "delete", f"watched {days}d ago by {rule.watched_by}"))

    return results


def run_evaluation(dry_run: bool = True) -> EngineReport:
    """Evaluate all Plex items against rules. Returns report of actions."""
    from mediacleaner.config import get_config
    report = EngineReport()
    session = get_session()
    cfg = get_config()
    excluded = cfg.get("maintenance", {}).get("excluded_libraries", [])

    try:
        libraries = plex.get_libraries()
    except Exception as e:
        report.errors.append(f"Failed to connect to Plex: {e}")
        return report

    for lib_name in libraries:
        if lib_name in excluded:
            continue

        try:
            items = plex.get_library_items(lib_name)
        except Exception as e:
            report.errors.append(f"Failed to list library '{lib_name}': {e}")
            continue

        for item in items:
            key = str(item.ratingKey)
            title = item.title

            rule = resolve_rule(item, lib_name)
            if rule is None:
                continue  # no rule, default keep

            manager, manager_id = find_manager(item)

            # Show-scoped rules evaluate per-episode
            if rule.scope == "show" and hasattr(item, "episodes"):
                for ep, action, reason in evaluate_show_episodes(item, rule):
                    if action == "delete_show":
                        result = EvalResult(
                            title=title, rating_key=key, action="delete",
                            rule_id=rule.id, reason=reason,
                            manager=manager, manager_id=manager_id,
                        )
                        report.results.append(result)
                        if action != "keep":
                            session.add(ActionLog(
                                media_title=title, plex_rating_key=key,
                                rule_id=rule.id, action_taken="delete", dry_run=dry_run,
                                details=json.dumps({"reason": reason, "manager": manager, "scope": "whole_show"}),
                            ))
                        break
                    elif action == "pending_confirm":
                        _handle_pending_confirm(session, rule, key, title, dry_run)
                        report.results.append(EvalResult(
                            title=title, rating_key=key, action="pending_confirm",
                            rule_id=rule.id, reason=reason, manager=manager, manager_id=manager_id,
                        ))
                        break
                    else:
                        ep_title = f"{title} - S{ep.parentIndex:02d}E{ep.index:02d}" if action == "delete" else title
                        result = EvalResult(
                            title=ep_title if action == "delete" else title,
                            rating_key=str(ep.ratingKey) if action == "delete" else key,
                            action=action, rule_id=rule.id, reason=reason,
                            manager=manager, manager_id=manager_id,
                        )
                        report.results.append(result)
                        if action == "delete":
                            session.add(ActionLog(
                                media_title=ep_title, plex_rating_key=str(ep.ratingKey),
                                rule_id=rule.id, action_taken="delete", dry_run=dry_run,
                                details=json.dumps({"reason": reason, "manager": manager}),
                            ))
                continue

            action, reason = evaluate_item(item, rule)
            result = EvalResult(
                title=title, rating_key=key, action=action,
                rule_id=rule.id, reason=reason,
                manager=manager, manager_id=manager_id,
            )
            report.results.append(result)

            if action == "delete":
                session.add(ActionLog(
                    media_title=title, plex_rating_key=key, rule_id=rule.id,
                    action_taken="delete", dry_run=dry_run,
                    details=json.dumps({"reason": reason, "manager": manager}),
                ))

    session.commit()
    session.close()
    return report


def run_orphan_scan() -> list[EvalResult]:
    """Separate orphan detection scan. Returns list of orphaned items."""
    from mediacleaner.config import get_config
    cfg = get_config()
    excluded = cfg.get("maintenance", {}).get("excluded_libraries", [])
    orphans = []

    try:
        libraries = plex.get_libraries()
    except Exception:
        return orphans

    for lib_name in libraries:
        if lib_name in excluded:
            continue
        try:
            items = plex.get_library_items(lib_name)
        except Exception:
            continue
        for item in items:
            manager, _ = find_manager(item)
            if manager == "none":
                orphans.append(EvalResult(
                    title=item.title, rating_key=str(item.ratingKey),
                    action="orphan_detected", reason=f"not managed (library: {lib_name})",
                ))
    return orphans


def execute_deletions(report: EngineReport):
    """Actually perform deletions for items marked 'delete' in the report."""
    for result in report.results:
        if result.action != "delete":
            continue

        try:
            if result.manager == "sonarr":
                # Check if this is an episode (show-scoped rule) vs whole series
                _delete_via_sonarr(result)
            elif result.manager == "radarr":
                radarr.delete_movie(result.manager_id, delete_files=True)
                ombi.cleanup_for_title(result.title)
            elif result.manager == "medusa":
                medusa.delete_show(result.manager_id, remove_files=True)
                ombi.cleanup_for_title(result.title)

            log.info(f"Deleted: {result.title} via {result.manager}")
        except Exception as e:
            log.error(f"Failed to delete {result.title}: {e}")
            report.errors.append(f"Delete failed for {result.title}: {e}")


def _delete_via_sonarr(result: EvalResult):
    """Delete via Sonarr — either a whole series or a single episode file."""
    # If title contains S##E## pattern, it's an episode deletion
    if " - S" in result.title and "E" in result.title:
        # Find the episode file by matching the Plex file path
        ep_files = sonarr.get_episode_files(result.manager_id)
        # Get the plex file path for this episode
        server = plex._server()
        try:
            plex_item = server.fetchItem(int(result.rating_key))
            ep_paths = plex.get_file_paths(plex_item)
        except Exception:
            ep_paths = []

        for ef in ep_files:
            if ef.get("path") and ef["path"] in ep_paths:
                sonarr.delete_episode_file(ef["id"])
                return
        # Fallback: couldn't match file, log warning
        log.warning(f"Could not match episode file for {result.title}")
    else:
        sonarr.delete_series(result.manager_id, delete_files=True)
        ombi.cleanup_for_title(result.title)


# --- Confirmation workflow ---


def _handle_pending_confirm(session, rule: Rule, rating_key: str, title: str, dry_run: bool):
    """Create or skip a pending confirmation for an item."""
    # Check if already pending
    existing = session.execute(
        select(PendingAction).where(
            PendingAction.plex_rating_key == rating_key,
            PendingAction.confirmed == False,
            PendingAction.cancelled == False,
        )
    ).scalar_one_or_none()

    if existing:
        return  # already waiting for confirmation

    if dry_run:
        log.info(f"Would send confirmation for: {title}")
        return

    token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    expires = now + timedelta(days=rule.confirm_days)

    session.add(PendingAction(
        rule_id=rule.id, plex_rating_key=rating_key, media_title=title,
        token=token, confirm_method=rule.confirm_method or "url_click",
        notified_at=now, expires_at=expires,
    ))
    session.flush()

    _send_confirmation_email(rule, title, token)


def _send_confirmation_email(rule: Rule, title: str, token: str):
    """Send confirmation email based on the rule's confirm_method."""
    from mediacleaner.config import get_config
    cfg = get_config()
    base_url = f"http://localhost:{cfg['web'].get('port', 9393)}"

    method = rule.confirm_method or "url_click"
    if method == "url_click":
        body = (
            f'"{title}" is scheduled for deletion.\n\n'
            f"If you want to KEEP it, click this link within {rule.confirm_days} days:\n"
            f"{base_url}/confirm/keep/{token}\n\n"
            f"If you do nothing, it will be deleted after the confirmation period."
        )
    elif method == "start_watching":
        body = (
            f'"{title}" is scheduled for deletion.\n\n'
            f"If you want to KEEP it, start watching any episode within {rule.confirm_days} days.\n"
            f"Any playback activity will cancel the deletion.\n\n"
            f"If you do nothing, it will be deleted after the confirmation period."
        )
    elif method == "mark_unwatched":
        body = (
            f'"{title}" is scheduled for deletion.\n\n'
            f"If you want to KEEP it, mark any episode as unwatched within {rule.confirm_days} days.\n\n"
            f"If you do nothing, it will be deleted after the confirmation period."
        )
    else:
        body = f'"{title}" is scheduled for deletion in {rule.confirm_days} days.'

    subject = f"MediaCleaner: {title} scheduled for deletion"
    recipient = rule.confirm_email
    if recipient:
        notify.send_to(subject, body, recipient)
    else:
        notify.send(subject, body)


def process_pending_actions():
    """Check pending actions: cancel if user intervened, delete if expired."""
    session = get_session()
    now = datetime.now(timezone.utc)

    pending = session.execute(
        select(PendingAction).where(
            PendingAction.confirmed == False,
            PendingAction.cancelled == False,
        )
    ).scalars().all()

    for pa in pending:
        # Check if user cancelled via Plex activity
        if _user_cancelled_via_plex(pa):
            pa.cancelled = True
            log.info(f"Pending deletion cancelled by user activity: {pa.media_title}")
            session.add(ActionLog(
                media_title=pa.media_title, plex_rating_key=pa.plex_rating_key,
                rule_id=pa.rule_id, action_taken="confirm_cancelled",
                details=json.dumps({"method": pa.confirm_method}),
            ))
            continue

        # If expired and not cancelled, confirm for deletion
        if now >= pa.expires_at:
            pa.confirmed = True
            log.info(f"Confirmation expired, marking for deletion: {pa.media_title}")
            session.add(ActionLog(
                media_title=pa.media_title, plex_rating_key=pa.plex_rating_key,
                rule_id=pa.rule_id, action_taken="confirm_expired_delete",
                details=json.dumps({"method": pa.confirm_method}),
            ))

    session.commit()
    session.close()


def get_confirmed_deletions() -> list[PendingAction]:
    """Return pending actions that have been confirmed (expired without cancellation)."""
    session = get_session()
    results = session.execute(
        select(PendingAction).where(PendingAction.confirmed == True)
    ).scalars().all()
    session.close()
    return results


def _user_cancelled_via_plex(pa: PendingAction) -> bool:
    """Check if user activity in Plex should cancel the pending deletion."""
    if pa.confirm_method == "url_click":
        return False  # only URL click cancels, handled by web route

    try:
        server = plex._server()
        item = server.fetchItem(int(pa.plex_rating_key))
    except Exception:
        return False

    if pa.confirm_method == "start_watching":
        # Check if any episode has been viewed since the notification
        if hasattr(item, "episodes"):
            for ep in item.episodes():
                viewed = getattr(ep, "lastViewedAt", None)
                if viewed and viewed.replace(tzinfo=timezone.utc) > pa.notified_at:
                    return True
        else:
            viewed = getattr(item, "lastViewedAt", None)
            if viewed and viewed.replace(tzinfo=timezone.utc) > pa.notified_at:
                return True

    elif pa.confirm_method == "mark_unwatched":
        # If any episode is now unwatched, user marked it
        if hasattr(item, "episodes"):
            for ep in item.episodes():
                if not ep.isWatched:
                    return True
        elif not item.isWatched:
            return True

    return False


def cancel_pending_by_token(token: str) -> bool:
    """Cancel a pending deletion via URL token. Returns True if found."""
    session = get_session()
    pa = session.execute(
        select(PendingAction).where(PendingAction.token == token)
    ).scalar_one_or_none()
    if pa and not pa.confirmed:
        pa.cancelled = True
        session.commit()
        session.close()
        return True
    session.close()
    return False
