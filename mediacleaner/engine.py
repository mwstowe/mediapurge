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
    notified_at: str | None = None
    file_size: int = 0  # bytes


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

    # Auto-approve Ombi requests for media that exists in Plex
    try:
        plex_ids = {"tvdb": set(), "imdb": set(), "tmdb": set()}
        for lib_name, _ in plex.get_libraries():
            for item in plex.get_library_items(lib_name):
                for guid in getattr(item, "guids", []):
                    gid = guid.id  # e.g. "tvdb://322191"
                    if gid.startswith("tvdb://"):
                        plex_ids["tvdb"].add(int(gid[7:]))
                    elif gid.startswith("imdb://"):
                        plex_ids["imdb"].add(gid[7:])
                    elif gid.startswith("tmdb://"):
                        plex_ids["tmdb"].add(int(gid[7:]))
        approved = ombi.approve_managed_requests(plex_ids)
        if approved:
            log.info(f"Auto-approved Ombi requests: {approved}")
    except Exception as e:
        log.warning(f"Ombi approval sync failed: {e}")


def _is_show_ended(show) -> bool:
    """Check if a show has ended by querying the managing app."""
    paths = plex.get_file_paths(show)
    if not paths:
        return False
    path = paths[0]
    try:
        for s in sonarr.get_all_series():
            if path.startswith(s["path"]):
                return s.get("ended", s.get("status") == "ended")
    except Exception:
        pass
    try:
        for s in medusa.get_all_shows():
            show_path = s.get("config", {}).get("location", "")
            if path.startswith(show_path):
                return s.get("status", "").lower() == "ended"
    except Exception:
        pass
    return False


def find_manager(item) -> tuple[str, int | None]:
    """Determine which application manages a Plex item. Prefers medusa > sonarr > radarr."""
    paths = plex.get_file_paths(item)
    if not paths:
        return "none", None

    session = get_session()
    managed = session.execute(select(ManagedMedia)).scalars().all()
    session.close()

    matches = []
    for m in managed:
        if not m.file_path:
            continue
        for p in paths:
            if p.startswith(m.file_path):
                matches.append((m.manager, m.manager_id))
                break

    if not matches:
        return "none", None

    # Priority: medusa > sonarr > radarr
    priority = {"medusa": 0, "sonarr": 1, "radarr": 2}
    matches.sort(key=lambda x: priority.get(x[0], 99))
    return matches[0]


def resolve_rules(item, library_name: str) -> list[Rule]:
    """Find all matching rules at the most specific scope: episode > season > show > library."""
    session = get_session()
    key = str(item.ratingKey)

    # Episode-specific rules
    rules = session.execute(
        select(Rule).where(Rule.scope == "episode", Rule.plex_rating_key == key, Rule.enabled == True)
    ).scalars().all()

    if not rules:
        # Season-level rules
        season_key = str(getattr(item, "parentRatingKey", ""))
        if season_key:
            rules = session.execute(
                select(Rule).where(Rule.scope == "season", Rule.plex_rating_key == season_key, Rule.enabled == True)
            ).scalars().all()

    if not rules:
        # Show/movie-level rules
        show_key = str(getattr(item, "grandparentRatingKey", getattr(item, "ratingKey", "")))
        rules = session.execute(
            select(Rule).where(Rule.scope == "show", Rule.plex_rating_key == show_key, Rule.enabled == True)
        ).scalars().all()

    if not rules:
        # Library-level rules
        rules = session.execute(
            select(Rule).where(Rule.scope == "library", Rule.plex_library == library_name, Rule.enabled == True)
        ).scalars().all()

    session.close()
    return rules


def evaluate_item(item, rule: Rule) -> tuple[str, str]:
    """Evaluate a single non-show item against a rule. Returns (action, reason)."""
    if rule.action == "keep":
        return "keep", "rule action is keep"

    # Check if rule is snoozed
    if rule.snoozed_until and datetime.now(timezone.utc) < rule.snoozed_until.replace(tzinfo=timezone.utc):
        days_left = (rule.snoozed_until.replace(tzinfo=timezone.utc) - datetime.now(timezone.utc)).days
        return "keep", f"snoozed for {days_left} more days"

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

    # Check max_days_inactive (no activity for X days)
    if rule.max_days_inactive > 0:
        last_viewed_at = plex._to_utc(getattr(item, "lastViewedAt", None))
        added_at = plex._to_utc(item.addedAt)
        # Use the more recent of last watched or added (handles re-added media)
        last_activity = max(filter(None, [last_viewed_at, added_at]), default=None)
        if last_activity:
            inactive = (datetime.now(timezone.utc) - last_activity).days
            if inactive >= rule.max_days_inactive:
                label = f"inactive {inactive}d" if last_viewed_at else f"never watched, added {inactive}d ago"
                if rule.confirm_before_delete:
                    return "pending_confirm", _pending_reason(rule, label, str(item.ratingKey))
                return "delete", f"{label} (limit {rule.max_days_inactive}d)"
        return "keep", "not yet inactive"

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


def _pending_reason(rule: Rule, trigger: str, rating_key: str = None) -> str:
    """Format a pending_confirm reason with deadline and method info."""
    method = rule.confirm_method or "url_click"
    methods = {"url_click": "keeps via link", "start_watching": "starts watching", "mark_unwatched": "marks unwatched"}
    who = rule.watched_by if rule.watched_by != "any" else "anyone"

    # Check if there's already a pending action with a real expiry
    deadline = None
    if rating_key:
        session = get_session()
        pa = session.execute(
            select(PendingAction).where(
                PendingAction.plex_rating_key == rating_key,
                PendingAction.confirmed == False,
                PendingAction.cancelled == False,
            )
        ).scalar_one_or_none()
        session.close()
        if pa:
            deadline = pa.expires_at.strftime("%Y-%m-%d")

    if not deadline:
        from datetime import timedelta
        deadline = (datetime.now(timezone.utc) + timedelta(days=rule.confirm_days)).strftime("%Y-%m-%d")

    return f"{trigger} · deletes after {deadline} unless {who} {methods.get(method, method)}"


def _evaluate_by_season(show, rule: Rule) -> list[tuple]:
    """Evaluate a show season-by-season. Delete entire seasons where all eps are watched."""
    users = [u.strip() for u in rule.watched_by.split(",")]
    seasons = {}
    for ep in show.episodes():
        seasons.setdefault(ep.parentIndex, []).append(ep)

    # Sort season numbers descending (newest first) for min_episodes protection
    sorted_seasons = sorted(seasons.keys(), reverse=True)

    results = []
    protected = 0
    for snum in sorted_seasons:
        eps = seasons[snum]

        # Protect newest N seasons (using min_episodes as min_seasons here)
        if rule.min_episodes > 0 and protected < rule.min_episodes:
            results.append((show, "keep", f"S{snum:02d}: protected (newest {rule.min_episodes} seasons)"))
            protected += 1
            continue

        # Check if all episodes in this season are watched
        latest_viewed = None
        all_watched = True
        for ep in eps:
            if "any" in users:
                if not ep.isWatched:
                    all_watched = False
                    break
                viewed = plex._to_utc(getattr(ep, "lastViewedAt", None))
            else:
                watched, viewed = plex.is_watched_by(ep, users)
                if not watched:
                    all_watched = False
                    break
                viewed = plex._to_utc(viewed)
            if viewed and (latest_viewed is None or viewed > latest_viewed):
                latest_viewed = viewed

        if not all_watched:
            results.append((show, "keep", f"S{snum:02d}: not all episodes watched"))
            continue

        # Check grace period
        days = plex.days_since_watched(latest_viewed)
        if days is not None and days < rule.min_days_watched:
            results.append((show, "keep", f"S{snum:02d}: watched {days}d ago, need {rule.min_days_watched}d"))
            continue

        # Season eligible for deletion — return each episode as a delete
        for ep in eps:
            results.append((ep, "delete", f"S{snum:02d} fully watched {days}d ago"))

    return results


def evaluate_show_episodes(show, rule: Rule) -> list[tuple]:
    """Evaluate episodes of a show. Returns list of (item, action, reason).
    May return the show itself as the item if the whole show should be deleted."""
    if rule.action == "keep":
        return [(show, "keep", "rule action is keep")]

    # Check if rule is snoozed
    if rule.snoozed_until and datetime.now(timezone.utc) < rule.snoozed_until.replace(tzinfo=timezone.utc):
        days_left = (rule.snoozed_until.replace(tzinfo=timezone.utc) - datetime.now(timezone.utc)).days
        return [(show, "keep", f"snoozed for {days_left} more days")]

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
            return [(show, "pending_confirm", _pending_reason(rule, f"all watched {days}d ago", str(show.ratingKey)))]
        return [(show, "delete_show", f"all watched {days}d ago by {rule.watched_by}")]

    # Check delete_by_season — delete entire seasons where all episodes are watched
    if rule.delete_by_season:
        return _evaluate_by_season(show, rule)

    # Check show-level inactivity timeout (delete entire show)
    if rule.max_days_inactive > 0:
        inactive_days = plex.days_since_last_activity(show)
        if inactive_days is not None and inactive_days >= rule.max_days_inactive:
            if rule.confirm_before_delete:
                return [(show, "pending_confirm", _pending_reason(rule, f"inactive {inactive_days}d", str(show.ratingKey)))]
            return [(show, "delete_show", f"inactive {inactive_days}d (limit {rule.max_days_inactive}d)")]
        # If never watched and added long ago, also consider inactive
        if inactive_days is None:
            age = plex.days_since_added(show)
            if age >= rule.max_days_inactive:
                if rule.confirm_before_delete:
                    return [(show, "pending_confirm", _pending_reason(rule, f"never watched, added {age}d ago", str(show.ratingKey)))]
                return [(show, "delete_show", f"never watched, added {age}d ago (limit {rule.max_days_inactive}d)")]

    episodes = show.episodes()
    # Sort oldest first (by season + episode number)
    episodes.sort(key=lambda e: (e.parentIndex or 0, e.index or 0))

    # min_episodes: protect the LATEST N episodes from deletion
    protected_count = rule.min_episodes if rule.min_episodes > 0 else 0
    deletable = episodes[:len(episodes) - protected_count] if protected_count else episodes

    results = []
    users = [u.strip() for u in rule.watched_by.split(",")]

    # Process in order — stop at the first episode that doesn't qualify
    for ep in deletable:
        # Check on-deck
        if rule.protect_on_deck and plex.is_on_deck(ep):
            break  # stop here, don't skip

        # Check watched
        if "any" in users:
            watched, last_viewed = ep.isWatched, getattr(ep, "lastViewedAt", None)
        else:
            watched, last_viewed = plex.is_watched_by(ep, users)

        # Hard age limit overrides watch requirement
        if rule.max_days_age > 0:
            age = plex.days_since_added(ep)
            if age >= rule.max_days_age:
                results.append((ep, "delete", f"exceeded max age ({age}d)"))
                continue

        if not watched:
            break  # not watched — stop processing

        days = plex.days_since_watched(last_viewed)
        if days is None:
            break
        if days < rule.min_days_watched:
            break  # grace period not met — stop

        results.append((ep, "delete", f"watched {days}d ago by {rule.watched_by}"))

    # If confirm is set and we have deletes, convert them to pending
    if rule.confirm_before_delete and results:
        results = [
            (item, "pending_confirm" if action == "delete" else action, reason)
            for item, action, reason in results
        ]

    # If ALL deletable episodes qualify AND show has ended, collapse to show-level action
    if results and len(results) == len(deletable):
        show_ended = _is_show_ended(show)
        if show_ended:
            if rule.confirm_before_delete:
                return [(show, "pending_confirm", _pending_reason(rule, "all episodes eligible", str(show.ratingKey)))]
            return [(show, "delete_show", "all episodes eligible, show ended")]

    return results


def run_evaluation(dry_run: bool = True) -> EngineReport:
    """Evaluate all Plex items against rules. Returns report of actions."""
    from mediacleaner.config import get_config
    report = EngineReport()
    session = get_session()
    cfg = get_config()
    excluded = cfg.get("maintenance", {}).get("excluded_libraries", [])

    try:
        libraries = [name for name, _ in plex.get_libraries()]
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

            rule = resolve_rules(item, lib_name)
            if not rule:
                continue  # no rules, default keep

            manager, manager_id = find_manager(item)

            # Evaluate each matching rule; first one that triggers wins
            for r in rule:
                # Show-scoped rules evaluate per-episode
                if r.scope == "show" and hasattr(item, "episodes"):
                    triggered = False
                    pending_eps = []
                    for ep, action, reason in evaluate_show_episodes(item, r):
                        if action == "delete_show":
                            report.results.append(EvalResult(
                                title=title, rating_key=key, action="delete",
                                rule_id=r.id, reason=reason,
                                manager=manager, manager_id=manager_id,
                            ))
                            session.add(ActionLog(
                                media_title=title, plex_rating_key=key,
                                rule_id=r.id, action_taken="delete", dry_run=dry_run,
                                details=json.dumps({"reason": reason, "manager": manager, "scope": "whole_show"}),
                            ))
                            triggered = True
                            pending_eps = []
                            break
                        elif action == "pending_confirm" and getattr(ep, "type", "") != "episode":
                            _handle_pending_confirm(session, r, key, title, dry_run)
                            report.results.append(EvalResult(
                                title=title, rating_key=key, action="pending_confirm",
                                rule_id=r.id, reason=reason, manager=manager, manager_id=manager_id,
                            ))
                            triggered = True
                            pending_eps = []
                            break
                        elif action == "delete":
                            ep_title = f"{title} - S{ep.parentIndex:02d}E{ep.index:02d}"
                            report.results.append(EvalResult(
                                title=ep_title, rating_key=str(ep.ratingKey), action="delete",
                                rule_id=r.id, reason=reason,
                                manager=manager, manager_id=manager_id,
                            ))
                            session.add(ActionLog(
                                media_title=ep_title, plex_rating_key=str(ep.ratingKey),
                                rule_id=r.id, action_taken="delete", dry_run=dry_run,
                                details=json.dumps({"reason": reason, "manager": manager}),
                            ))
                            triggered = True
                        elif action == "pending_confirm":
                            pending_eps.append(f"S{ep.parentIndex:02d}E{ep.index:02d}")
                            triggered = True

                    # Group episode pending_confirms into one show-level notification
                    if pending_eps:
                        ep_list = ", ".join(pending_eps[:5])
                        if len(pending_eps) > 5:
                            ep_list += f" +{len(pending_eps) - 5} more"
                        reason = _pending_reason(r, f"{len(pending_eps)} eps ({ep_list})", key)
                        _handle_pending_confirm(session, r, key, title, dry_run)
                        report.results.append(EvalResult(
                            title=title, rating_key=key, action="pending_confirm",
                            rule_id=r.id, reason=reason, manager=manager, manager_id=manager_id,
                        ))

                    if triggered:
                        break
                    continue

                action, reason = evaluate_item(item, r)
                if action != "keep":
                    result = EvalResult(
                        title=title, rating_key=key, action=action,
                        rule_id=r.id, reason=reason,
                        manager=manager, manager_id=manager_id,
                    )
                    report.results.append(result)
                    if action == "delete":
                        session.add(ActionLog(
                            media_title=title, plex_rating_key=key, rule_id=r.id,
                            action_taken="delete", dry_run=dry_run,
                            details=json.dumps({"reason": reason, "manager": manager}),
                        ))
                    elif action == "pending_confirm":
                        _handle_pending_confirm(session, r, key, title, dry_run)
                    break  # first triggering rule wins

    session.commit()
    session.close()

    # Enrich pending_confirm results with notification status
    pa_session = get_session()
    for result in report.results:
        if result.action == "pending_confirm":
            pa = pa_session.execute(
                select(PendingAction).where(
                    PendingAction.plex_rating_key == result.rating_key,
                    PendingAction.confirmed == False,
                    PendingAction.cancelled == False,
                )
            ).scalar_one_or_none()
            if pa:
                result.notified_at = pa.notified_at.strftime("%Y-%m-%d %H:%M")
    pa_session.close()

    # Enrich results with file sizes
    server = plex._server()
    for result in report.results:
        if result.action in ("delete", "pending_confirm") and result.rating_key:
            try:
                item = server.fetchItem(int(result.rating_key))
                result.file_size = plex.get_file_size(item)
            except Exception:
                pass

    return report


def run_orphan_scan() -> list[EvalResult]:
    """Separate orphan detection scan. Returns list of orphaned items."""
    from mediacleaner.config import get_config
    cfg = get_config()
    excluded = cfg.get("maintenance", {}).get("excluded_libraries", [])
    orphans = []

    try:
        libraries = [name for name, _ in plex.get_libraries()]
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
    rules_to_delete = set()
    medusa_shows_refreshed = set()

    for result in report.results:
        if result.action != "delete":
            continue

        try:
            is_episode = " - S" in result.title
            if is_episode:
                _delete_episode(result)
                if result.manager == "medusa" and result.manager_id:
                    medusa_shows_refreshed.add(str(result.manager_id))
            elif result.manager == "sonarr":
                sonarr.delete_series(int(result.manager_id), delete_files=True)
                ombi.cleanup_for_title(result.title)
            elif result.manager == "radarr":
                radarr.delete_movie(int(result.manager_id), delete_files=True)
                ombi.cleanup_for_title(result.title)
            elif result.manager == "medusa":
                medusa.delete_show(str(result.manager_id), remove_files=True)
                ombi.cleanup_for_title(result.title)
            elif result.manager == "none":
                _delete_direct(result)

            log.info(f"Deleted: {result.title} via {result.manager}")

            # If this was a whole-show/movie deletion (not episode-level), retire the rule
            if result.rule_id and not is_episode:
                rules_to_delete.add(result.rule_id)

        except Exception as e:
            log.error(f"Failed to delete {result.title}: {e}")
            report.errors.append(f"Delete failed for {result.title}: {e}")

    # Clean up rules that are no longer needed
    if rules_to_delete:
        session = get_session()
        for rule_id in rules_to_delete:
            rule = session.get(Rule, rule_id)
            if rule:
                log.info(f"Retiring rule #{rule.id} ({rule.media_title}) — media deleted")
                session.delete(rule)
        session.commit()
        session.close()

    # Refresh Medusa shows that had episodes deleted (clears stale file info)
    for slug in medusa_shows_refreshed:
        try:
            medusa.refresh_show(slug)
        except Exception:
            pass

    # Trigger Plex library scan to reflect deletions
    if any(r.action == "delete" for r in report.results):
        try:
            for lib_name, _ in plex.get_libraries():
                plex.scan_library(lib_name)
            log.info("Triggered Plex library scan")
        except Exception as e:
            log.warning(f"Failed to trigger Plex scan: {e}")


def _delete_direct(result: EvalResult):
    """Delete unmanaged media directly from disk."""
    import os
    import shutil
    server = plex._server()
    try:
        plex_item = server.fetchItem(int(result.rating_key))
        paths = plex.get_file_paths(plex_item)
        for path in paths:
            if os.path.isdir(path):
                shutil.rmtree(path)
                log.info(f"Removed directory: {path}")
            elif os.path.exists(path):
                os.remove(path)
                log.info(f"Removed file: {path}")
    except Exception as e:
        log.warning(f"Could not remove files for {result.title}: {e}")


def _delete_episode(result: EvalResult):
    """Delete an episode file and mark as unmonitored/ignored in the managing app."""
    # Parse season/episode from title format "Show - S01E05"
    import re
    match = re.search(r"S(\d+)E(\d+)", result.title)
    season = int(match.group(1)) if match else None
    episode = int(match.group(2)) if match else None

    if result.manager == "sonarr":
        # Find and delete the episode file via Sonarr
        ep_files = sonarr.get_episode_files(int(result.manager_id))
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
        log.warning(f"Could not match episode file for {result.title}")

    elif result.manager == "medusa":
        # Mark as ignored in Medusa + delete file from disk
        if season is not None and episode is not None:
            medusa.ignore_episode(str(result.manager_id), season, episode)
        # Delete the actual file
        server = plex._server()
        try:
            plex_item = server.fetchItem(int(result.rating_key))
            for path in plex.get_file_paths(plex_item):
                import os
                if os.path.exists(path):
                    os.remove(path)
                    log.info(f"Removed file: {path}")
        except Exception as e:
            log.warning(f"Could not remove file for {result.title}: {e}")


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
    base_url = cfg["web"].get("base_url", f"https://localhost:{cfg['web'].get('port', 9393)}")

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
            # Snooze the rule
            rule = session.get(Rule, pa.rule_id)
            if rule:
                snooze_days = max(rule.max_days_inactive, rule.min_days_watched, rule.confirm_days)
                rule.snoozed_until = datetime.now(timezone.utc) + timedelta(days=snooze_days)
            log.info(f"Pending deletion cancelled by user activity: {pa.media_title}")
            session.add(ActionLog(
                media_title=pa.media_title, plex_rating_key=pa.plex_rating_key,
                rule_id=pa.rule_id, action_taken="confirm_cancelled",
                details=json.dumps({"method": pa.confirm_method}),
            ))
            continue

        # If expired and not cancelled, confirm for deletion
        if now >= pa.expires_at.replace(tzinfo=timezone.utc):
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
                if viewed and viewed.replace(tzinfo=timezone.utc) > pa.notified_at.replace(tzinfo=timezone.utc):
                    return True
        else:
            viewed = getattr(item, "lastViewedAt", None)
            if viewed and viewed.replace(tzinfo=timezone.utc) > pa.notified_at.replace(tzinfo=timezone.utc):
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
    """Cancel a pending deletion via URL token. Snoozes the rule for its full period."""
    session = get_session()
    pa = session.execute(
        select(PendingAction).where(PendingAction.token == token)
    ).scalar_one_or_none()
    if pa and not pa.confirmed:
        pa.cancelled = True
        # Snooze the rule so it won't re-trigger immediately
        rule = session.get(Rule, pa.rule_id)
        if rule:
            # Reset timer: snooze for the longer of max_days_inactive or confirm_days
            snooze_days = max(rule.max_days_inactive, rule.min_days_watched, rule.confirm_days)
            rule.snoozed_until = datetime.now(timezone.utc) + timedelta(days=snooze_days)
        session.commit()
        session.close()
        return True
    session.close()
    return False
