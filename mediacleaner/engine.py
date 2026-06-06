"""Rule evaluation engine — resolves rules, evaluates triggers, detects orphans."""

import json
import logging
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import joinedload

from mediacleaner.clients import medusa, plex, radarr, sonarr, ombi
from mediacleaner.db import get_session
from mediacleaner.models import ActionLog, ManagedMedia, PendingAction, Rule, Trigger
from mediacleaner import notify

log = logging.getLogger(__name__)


@dataclass
class EvalResult:
    title: str
    rating_key: str
    action: str  # keep, delete, pending_confirm, orphan_detected
    rule_id: int | None = None
    trigger_id: int | None = None
    reason: str = ""
    manager: str = "none"
    manager_id: int | None = None
    notified_at: str | None = None
    file_size: int = 0


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
                    gid = guid.id
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
    """Determine which application manages a Plex item."""
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
        mpath = m.file_path.rstrip("/")
        for p in paths:
            if p.rstrip("/") == mpath or p.startswith(mpath + "/"):
                matches.append((m.manager, m.manager_id))
                break

    if not matches:
        return "none", None

    priority = {"medusa": 0, "sonarr": 1, "radarr": 2}
    matches.sort(key=lambda x: priority.get(x[0], 99))
    return matches[0]


def resolve_rules(item, library_name: str) -> list[Rule]:
    """Find matching rules at the most specific scope: episode > season > show > library."""
    session = get_session()
    key = str(item.ratingKey)

    rules = session.execute(
        select(Rule).options(joinedload(Rule.triggers)).where(
            Rule.scope == "episode", Rule.plex_rating_key == key, Rule.enabled == True)
    ).unique().scalars().all()

    if not rules:
        season_key = str(getattr(item, "parentRatingKey", ""))
        if season_key:
            rules = session.execute(
                select(Rule).options(joinedload(Rule.triggers)).where(
                    Rule.scope == "season", Rule.plex_rating_key == season_key, Rule.enabled == True)
            ).unique().scalars().all()

    if not rules:
        show_key = str(getattr(item, "grandparentRatingKey", getattr(item, "ratingKey", "")))
        rules = session.execute(
            select(Rule).options(joinedload(Rule.triggers)).where(
                Rule.scope == "show", Rule.plex_rating_key == show_key, Rule.enabled == True)
        ).unique().scalars().all()

    if not rules:
        rules = session.execute(
            select(Rule).options(joinedload(Rule.triggers)).where(
                Rule.scope == "library", Rule.plex_library == library_name, Rule.enabled == True)
        ).unique().scalars().all()

    session.close()
    return rules


def _trigger_snoozed(trigger: Trigger) -> bool:
    """Check if a trigger is currently snoozed."""
    if trigger.snoozed_until:
        snoozed = trigger.snoozed_until
        if snoozed.tzinfo is None:
            snoozed = snoozed.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) < snoozed:
            return True
    return False


def _check_trigger(item, trigger: Trigger, rule: Rule) -> tuple[str | None, str]:
    """Evaluate a single trigger against an item. Returns (action, reason) or (None, '') if not fired."""
    if not trigger.enabled or _trigger_snoozed(trigger):
        return None, ""

    users = [u.strip() for u in rule.watched_by.split(",")]

    if trigger.type == "watched":
        if "any" in users:
            watched, last_viewed = item.isWatched, getattr(item, "lastViewedAt", None)
        else:
            watched, last_viewed = plex.is_watched_by(item, users)
        if not watched:
            return None, ""
        days = plex.days_since_watched(last_viewed)
        if days is None or days < trigger.days:
            return None, ""
        action = "pending_confirm" if trigger.action == "confirm" else "delete"
        return action, f"watched {days}d ago by {rule.watched_by}"

    elif trigger.type == "inactive":
        last_viewed_at = plex._to_utc(getattr(item, "lastViewedAt", None))
        added_at = plex._to_utc(item.addedAt)
        candidates = [t for t in [last_viewed_at, added_at] if t]
        if trigger.snoozed_until:
            s = trigger.snoozed_until
            if s.tzinfo is None:
                s = s.replace(tzinfo=timezone.utc)
            candidates.append(s)
        last_activity = max(candidates) if candidates else None
        if not last_activity:
            return None, ""
        inactive = (datetime.now(timezone.utc) - last_activity).days
        if inactive < trigger.days:
            return None, ""
        label = f"inactive {inactive}d" if last_viewed_at else f"never watched, added {inactive}d ago"
        action = "pending_confirm" if trigger.action == "confirm" else "delete"
        return action, f"{label} (limit {trigger.days}d)"

    elif trigger.type == "age":
        age = plex.days_since_added(item)
        if age < trigger.days:
            return None, ""
        action = "pending_confirm" if trigger.action == "confirm" else "delete"
        return action, f"exceeded max age ({age} >= {trigger.days} days)"

    return None, ""


def evaluate_item(item, rule: Rule) -> tuple[str, str, Trigger | None]:
    """Evaluate a single item against a rule's triggers. Returns (action, reason, trigger)."""
    if rule.action == "keep":
        return "keep", "rule action is keep", None

    if rule.snoozed_until:
        snoozed = rule.snoozed_until
        if snoozed.tzinfo is None:
            snoozed = snoozed.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) < snoozed:
            days_left = (snoozed - datetime.now(timezone.utc)).days
            return "keep", f"snoozed for {days_left} more days", None

    if rule.protect_on_deck and plex.is_on_deck(item):
        return "keep", "protected: on deck", None

    for trigger in rule.triggers:
        action, reason = _check_trigger(item, trigger, rule)
        if action:
            return action, reason, trigger

    return "keep", "no trigger fired", None


def _evaluate_by_season(show, rule: Rule) -> list[tuple]:
    """Evaluate a show season-by-season."""
    users = [u.strip() for u in rule.watched_by.split(",")]
    seasons = {}
    for ep in show.episodes():
        seasons.setdefault(ep.parentIndex, []).append(ep)

    sorted_seasons = sorted(seasons.keys(), reverse=True)
    results = []
    protected = 0

    for snum in sorted_seasons:
        eps = seasons[snum]
        if rule.min_episodes > 0 and protected < rule.min_episodes:
            results.append((show, "keep", f"S{snum:02d}: protected (newest {rule.min_episodes} seasons)", None))
            protected += 1
            continue

        # Find best trigger that fires for the whole season
        best_trigger = None
        best_action = None
        best_reason = None
        for trigger in rule.triggers:
            if not trigger.enabled or _trigger_snoozed(trigger):
                continue
            season_ok = True
            latest_viewed = None
            for ep in eps:
                if trigger.type == "watched":
                    if "any" in users:
                        if not ep.isWatched:
                            season_ok = False
                            break
                        viewed = plex._to_utc(getattr(ep, "lastViewedAt", None))
                    else:
                        watched, viewed_raw = plex.is_watched_by(ep, users)
                        if not watched:
                            season_ok = False
                            break
                        viewed = plex._to_utc(viewed_raw)
                    if viewed and (latest_viewed is None or viewed > latest_viewed):
                        latest_viewed = viewed
                elif trigger.type == "age":
                    age = plex.days_since_added(ep)
                    if age < trigger.days:
                        season_ok = False
                        break
                elif trigger.type == "inactive":
                    last_viewed_at = plex._to_utc(getattr(ep, "lastViewedAt", None))
                    added_at = plex._to_utc(ep.addedAt)
                    candidates = [t for t in [last_viewed_at, added_at] if t]
                    last_activity = max(candidates) if candidates else None
                    if not last_activity or (datetime.now(timezone.utc) - last_activity).days < trigger.days:
                        season_ok = False
                        break

            if not season_ok:
                continue

            if trigger.type == "watched":
                days = plex.days_since_watched(latest_viewed)
                if days is not None and days >= trigger.days:
                    best_trigger = trigger
                    best_action = "pending_confirm" if trigger.action == "confirm" else "delete"
                    best_reason = f"S{snum:02d} fully watched {days}d ago"
                    break
            else:
                best_trigger = trigger
                best_action = "pending_confirm" if trigger.action == "confirm" else "delete"
                best_reason = f"S{snum:02d} trigger {trigger.type} fired"
                break

        if best_action:
            for ep in eps:
                results.append((ep, best_action, best_reason, best_trigger))
        else:
            results.append((show, "keep", f"S{snum:02d}: no trigger fired", None))

    return results


def evaluate_show_episodes(show, rule: Rule) -> list[tuple]:
    """Evaluate episodes of a show. Returns list of (item, action, reason, trigger)."""
    if rule.action == "keep":
        return [(show, "keep", "rule action is keep", None)]

    if rule.snoozed_until:
        snoozed = rule.snoozed_until
        if snoozed.tzinfo is None:
            snoozed = snoozed.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) < snoozed:
            days_left = (snoozed - datetime.now(timezone.utc)).days
            return [(show, "keep", f"snoozed for {days_left} more days", None)]

    # Season-mode processing
    if rule.processing_mode == "season":
        return _evaluate_by_season(show, rule)

    # Check show-level inactivity triggers first
    for trigger in rule.triggers:
        if not trigger.enabled or _trigger_snoozed(trigger):
            continue
        if trigger.type == "inactive":
            inactive_days = plex.days_since_last_activity(show)
            if inactive_days is not None and inactive_days >= trigger.days:
                action = "pending_confirm" if trigger.action == "confirm" else "delete_show"
                return [(show, action, f"inactive {inactive_days}d (limit {trigger.days}d)", trigger)]
            if inactive_days is None:
                age = plex.days_since_added(show)
                if age >= trigger.days:
                    action = "pending_confirm" if trigger.action == "confirm" else "delete_show"
                    return [(show, action, f"never watched, added {age}d ago (limit {trigger.days}d)", trigger)]

    # Episode-mode: process oldest first, stop at first non-qualifying
    episodes = show.episodes()
    episodes.sort(key=lambda e: (e.parentIndex or 0, e.index or 0))

    protected_count = rule.min_episodes if rule.min_episodes > 0 else 0
    deletable = episodes[:len(episodes) - protected_count] if protected_count else episodes

    results = []
    for ep in deletable:
        if rule.protect_on_deck and plex.is_on_deck(ep):
            break

        fired = False
        for trigger in rule.triggers:
            action, reason = _check_trigger(ep, trigger, rule)
            if action:
                results.append((ep, action, reason, trigger))
                fired = True
                break
        if not fired:
            break  # stop at first episode that doesn't qualify

    # If ALL deletable episodes qualify AND show has ended, collapse to show-level
    if results and len(results) == len(deletable):
        show_ended = _is_show_ended(show)
        if show_ended:
            # Use first trigger's action type
            t = results[0][3]
            if t and t.action == "confirm":
                return [(show, "pending_confirm", "all episodes eligible, show ended", t)]
            return [(show, "delete_show", "all episodes eligible, show ended", t)]

    return results


def run_evaluation(dry_run: bool = True) -> EngineReport:
    """Evaluate all Plex items against rules."""
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

            rules = resolve_rules(item, lib_name)
            if not rules:
                continue

            manager, manager_id = find_manager(item)

            for r in rules:
                # Show-scoped rules evaluate per-episode
                if r.scope in ("show", "library") and hasattr(item, "episodes"):
                    triggered = False
                    pending_eps = []
                    for ep, action, reason, trigger in evaluate_show_episodes(item, r):
                        if action == "delete_show":
                            report.results.append(EvalResult(
                                title=title, rating_key=key, action="delete",
                                rule_id=r.id, trigger_id=trigger.id if trigger else None,
                                reason=reason, manager=manager, manager_id=manager_id,
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
                            _handle_pending_confirm(session, r, trigger, key, title, dry_run)
                            report.results.append(EvalResult(
                                title=title, rating_key=key, action="pending_confirm",
                                rule_id=r.id, trigger_id=trigger.id if trigger else None,
                                reason=reason, manager=manager, manager_id=manager_id,
                            ))
                            triggered = True
                            pending_eps = []
                            break
                        elif action == "delete":
                            ep_title = f"{title} - S{ep.parentIndex:02d}E{ep.index:02d}"
                            report.results.append(EvalResult(
                                title=ep_title, rating_key=str(ep.ratingKey), action="delete",
                                rule_id=r.id, trigger_id=trigger.id if trigger else None,
                                reason=reason, manager=manager, manager_id=manager_id,
                            ))
                            session.add(ActionLog(
                                media_title=ep_title, plex_rating_key=str(ep.ratingKey),
                                rule_id=r.id, action_taken="delete", dry_run=dry_run,
                                details=json.dumps({"reason": reason, "manager": manager}),
                            ))
                            triggered = True
                        elif action == "pending_confirm":
                            pending_eps.append((ep, trigger))
                            triggered = True

                    if pending_eps:
                        ep_labels = [f"S{ep.parentIndex:02d}E{ep.index:02d}" for ep, _ in pending_eps[:5]]
                        ep_list = ", ".join(ep_labels)
                        if len(pending_eps) > 5:
                            ep_list += f" +{len(pending_eps) - 5} more"
                        t = pending_eps[0][1]
                        reason = f"{len(pending_eps)} eps ({ep_list}) · confirms in {t.confirm_days}d"
                        _handle_pending_confirm(session, r, t, key, title, dry_run)
                        report.results.append(EvalResult(
                            title=title, rating_key=key, action="pending_confirm",
                            rule_id=r.id, trigger_id=t.id if t else None,
                            reason=reason, manager=manager, manager_id=manager_id,
                        ))

                    if triggered:
                        break
                    continue

                # Single-item evaluation (movie or episode-scoped)
                action, reason, trigger = evaluate_item(item, r)
                if action != "keep":
                    result = EvalResult(
                        title=title, rating_key=key, action=action,
                        rule_id=r.id, trigger_id=trigger.id if trigger else None,
                        reason=reason, manager=manager, manager_id=manager_id,
                    )
                    report.results.append(result)
                    if action == "delete":
                        session.add(ActionLog(
                            media_title=title, plex_rating_key=key, rule_id=r.id,
                            action_taken="delete", dry_run=dry_run,
                            details=json.dumps({"reason": reason, "manager": manager}),
                        ))
                    elif action == "pending_confirm":
                        _handle_pending_confirm(session, r, trigger, key, title, dry_run)
                    break

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
    """Separate orphan detection scan."""
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
                _delete_direct(result)
                ombi.cleanup_for_title(result.title)
            elif result.manager == "none":
                _delete_direct(result)

            log.info(f"Deleted: {result.title} via {result.manager}")
            if result.rule_id and not is_episode:
                rules_to_delete.add(result.rule_id)
        except Exception as e:
            log.error(f"Failed to delete {result.title}: {e}")
            report.errors.append(f"Delete failed for {result.title}: {e}")

    if rules_to_delete:
        session = get_session()
        for rule_id in rules_to_delete:
            rule = session.get(Rule, rule_id)
            if rule:
                log.info(f"Retiring rule #{rule.id} ({rule.media_title}) — media deleted")
                session.delete(rule)
        session.commit()
        session.close()

    for slug in medusa_shows_refreshed:
        try:
            medusa.refresh_show(slug)
        except Exception:
            pass

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
    """Delete an episode file and mark as unmonitored/ignored."""
    import re
    match = re.search(r"S(\d+)E(\d+)", result.title)
    season = int(match.group(1)) if match else None
    episode = int(match.group(2)) if match else None

    if result.manager == "sonarr":
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
                try:
                    series_eps = sonarr.get_episodes(int(result.manager_id))
                    ep_ids = [e["id"] for e in series_eps
                              if e["seasonNumber"] == season and e["episodeNumber"] == episode]
                    if ep_ids:
                        sonarr.unmonitor_episodes(ep_ids)
                    season_eps = [e for e in series_eps if e["seasonNumber"] == season]
                    if all(not e["monitored"] or e["id"] in ep_ids for e in season_eps):
                        sonarr.unmonitor_season(int(result.manager_id), season)
                except Exception as e:
                    log.warning(f"Could not unmonitor in Sonarr: {e}")
                return
        log.warning(f"Could not match episode file for {result.title}")

    elif result.manager == "medusa":
        if season is not None and episode is not None:
            medusa.ignore_episode(str(result.manager_id), season, episode)
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


def _handle_pending_confirm(session, rule: Rule, trigger: Trigger | None, rating_key: str, title: str, dry_run: bool):
    """Create or skip a pending confirmation for an item."""
    existing = session.execute(
        select(PendingAction).where(
            PendingAction.plex_rating_key == rating_key,
            PendingAction.confirmed == False,
            PendingAction.cancelled == False,
        )
    ).scalar_one_or_none()

    if existing:
        return

    if dry_run:
        log.info(f"Would send confirmation for: {title}")
        return

    token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    confirm_days = trigger.confirm_days if trigger else 7
    expires = now + timedelta(days=confirm_days)

    session.add(PendingAction(
        rule_id=rule.id,
        trigger_id=trigger.id if trigger else None,
        plex_rating_key=rating_key, media_title=title,
        token=token,
        confirm_method=trigger.confirm_methods if trigger else "snooze",
        notified_at=now, expires_at=expires,
    ))
    session.flush()

    _send_confirmation_email(rule, trigger, title, token)


def _send_confirmation_email(rule: Rule, trigger: Trigger | None, title: str, token: str):
    """Send confirmation email listing available methods from trigger.confirm_methods."""
    from mediacleaner.config import get_config
    cfg = get_config()
    base_url = cfg["web"].get("base_url", f"https://localhost:{cfg['web'].get('port', 9393)}")

    methods = (trigger.confirm_methods if trigger else "snooze").split(",")
    confirm_days = trigger.confirm_days if trigger else 7
    is_movie = rule.scope == "library" or rule.processing_mode == "episode"
    # Determine if this is a movie by checking if it has episodes
    if rule.plex_rating_key:
        try:
            item = plex._server().fetchItem(int(rule.plex_rating_key))
            is_movie = item.type == "movie"
        except Exception:
            pass

    lines = [f'"{title}" is scheduled for deletion.\n']
    lines.append(f"You have {confirm_days} days to keep it. Options:\n")

    if "snooze" in methods:
        lines.append(f"  • Snooze (reset timer): {base_url}/confirm/snooze/{token}")
    if "disable" in methods:
        lines.append(f"  • Disable this trigger: {base_url}/confirm/disable/{token}")
    if "mark_unwatched" in methods:
        lines.append(f"  • Mark as unwatched: {base_url}/confirm/unwatched/{token}")
    if "start_watching" in methods:
        if is_movie:
            lines.append("  • Start watching (any playback cancels deletion)")
        else:
            lines.append("  • Start watching any episode (playback cancels deletion)")

    lines.append(f"\nIf you do nothing, it will be deleted after {(datetime.now(timezone.utc) + timedelta(days=confirm_days)).strftime('%B %d, %Y')}.")

    body = "\n".join(lines)
    subject = f"MediaCleaner: {title} scheduled for deletion"
    recipient = trigger.confirm_email if trigger and trigger.confirm_email else None
    if not recipient:
        # Resolve from watched_by -> user_emails config
        user_emails = cfg.get("plex", {}).get("user_emails", {})
        users = [u.strip() for u in rule.watched_by.split(",") if u.strip() != "any"]
        recipients = [user_emails[u] for u in users if u in user_emails]
        if recipients:
            for r in recipients:
                notify.send_to(subject, body, r)
            return
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
        if _user_cancelled_via_plex(pa):
            pa.cancelled = True
            # Snooze the trigger
            if pa.trigger_id:
                trigger = session.get(Trigger, pa.trigger_id)
                if trigger:
                    trigger.snoozed_until = datetime.now(timezone.utc) + timedelta(days=trigger.confirm_days)
            log.info(f"Pending deletion cancelled by user activity: {pa.media_title}")
            session.add(ActionLog(
                media_title=pa.media_title, plex_rating_key=pa.plex_rating_key,
                rule_id=pa.rule_id, action_taken="confirm_cancelled",
                details=json.dumps({"method": pa.confirm_method}),
            ))
            continue

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
    methods = pa.confirm_method.split(",") if pa.confirm_method else []
    # If only snooze/disable methods, no plex-based cancellation
    if not any(m in methods for m in ("start_watching", "mark_unwatched")):
        return False

    try:
        server = plex._server()
        item = server.fetchItem(int(pa.plex_rating_key))
    except Exception:
        return False

    if "start_watching" in methods:
        if hasattr(item, "episodes"):
            for ep in item.episodes():
                viewed = getattr(ep, "lastViewedAt", None)
                if viewed and viewed.replace(tzinfo=timezone.utc) > pa.notified_at.replace(tzinfo=timezone.utc):
                    return True
        else:
            viewed = getattr(item, "lastViewedAt", None)
            if viewed and viewed.replace(tzinfo=timezone.utc) > pa.notified_at.replace(tzinfo=timezone.utc):
                return True

    if "mark_unwatched" in methods:
        if hasattr(item, "episodes"):
            for ep in item.episodes():
                if not ep.isWatched:
                    return True
        elif not item.isWatched:
            return True

    return False


def cancel_pending_by_token(token: str, action: str = "snooze") -> bool:
    """Cancel a pending deletion via URL token. Action: snooze, disable, unwatched."""
    session = get_session()
    pa = session.execute(
        select(PendingAction).where(PendingAction.token == token)
    ).scalar_one_or_none()
    if not pa or pa.confirmed:
        session.close()
        return False

    pa.cancelled = True

    if action == "disable" and pa.trigger_id:
        trigger = session.get(Trigger, pa.trigger_id)
        if trigger:
            trigger.enabled = False
    elif action == "snooze" and pa.trigger_id:
        trigger = session.get(Trigger, pa.trigger_id)
        if trigger:
            trigger.snoozed_until = datetime.now(timezone.utc) + timedelta(days=trigger.confirm_days)
    elif action == "unwatched":
        # Mark as unwatched in Plex
        try:
            server = plex._server()
            item = server.fetchItem(int(pa.plex_rating_key))
            item.markUnwatched()
        except Exception as e:
            log.warning(f"Could not mark unwatched: {e}")
        # Also snooze the trigger
        if pa.trigger_id:
            trigger = session.get(Trigger, pa.trigger_id)
            if trigger:
                trigger.snoozed_until = datetime.now(timezone.utc) + timedelta(days=trigger.confirm_days)

    session.commit()
    session.close()
    return True
