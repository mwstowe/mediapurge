import functools
import os

import bcrypt
from flask import Flask, flash, redirect, render_template, request, session, url_for

from mediapurge.config import get_config, load_config
from mediapurge.db import get_session, init_db
from mediapurge.engine import run_evaluation, sync_managed_media
from mediapurge.models import ActionLog, ManagedMedia, Rule, Trigger
from mediapurge.clients import sonarr, radarr, medusa

from sqlalchemy import select, desc
from sqlalchemy.orm import joinedload


def create_app() -> Flask:
    load_config()
    init_db()
    cfg = get_config()

    app = Flask(__name__)
    app.secret_key = cfg["web"]["secret_key"]

    @app.template_filter("humansize")
    def humansize(value):
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if abs(value) < 1024:
                return f"{value:.1f} {unit}"
            value /= 1024
        return f"{value:.1f} PB"

    @app.template_filter("format_details")
    def format_details(value):
        if not value:
            return ""
        try:
            import json
            data = json.loads(value)
            parts = []
            if "reason" in data:
                parts.append(data["reason"])
            if "manager" in data:
                parts.append(f"via {data['manager']}")
            if "move_to" in data:
                parts.append(f"→ {data['move_to']}")
            if "scope" in data:
                parts.append(f"({data['scope']})")
            if "method" in data:
                parts.append(f"method: {data['method']}")
            if "library" in data:
                parts.append(f"library: {data['library']}")
            if parts:
                return " · ".join(parts)
            return " · ".join(f"{k}: {v}" for k, v in data.items())
        except (ValueError, TypeError):
            return value

    def login_required(f):
        @functools.wraps(f)
        def wrapped(*args, **kwargs):
            if not session.get("authed"):
                return redirect(url_for("login"))
            return f(*args, **kwargs)
        return wrapped

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            pw = request.form["password"].encode()
            stored = cfg["web"]["admin_password"].encode()
            if bcrypt.checkpw(pw, stored):
                session["authed"] = True
                return redirect(url_for("dashboard"))
            return render_template("login.html", error="Invalid password")
        return render_template("login.html")

    @app.route("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.route("/")
    @login_required
    def dashboard():
        db = get_session()
        rule_count = db.query(Rule).count()
        with _orphan_lock:
            orphan_count = len(_orphan_task["results"]) if _orphan_task["results"] else 0
        recent = db.execute(
            select(ActionLog).order_by(desc(ActionLog.timestamp)).limit(20)
        ).scalars().all()
        db.close()
        return render_template("dashboard.html", rule_count=rule_count,
                               orphan_count=orphan_count, recent=recent)

    @app.route("/rules")
    @login_required
    def rules_list():
        db = get_session()
        rules = db.execute(select(Rule).order_by(Rule.scope, Rule.plex_library)).scalars().all()
        db.close()
        # Determine display scope (show vs movie) for show-scoped rules
        from mediapurge.clients import plex as plex_client
        server = None
        for rule in rules:
            if rule.scope == "show" and rule.plex_rating_key:
                try:
                    if server is None:
                        server = plex_client._server()
                    item = server.fetchItem(int(rule.plex_rating_key))
                    rule.display_scope = "movie" if item.type == "movie" else "show"
                except Exception:
                    rule.display_scope = "show"
            else:
                rule.display_scope = rule.scope
        return render_template("rules.html", rules=rules)

    def _parse_triggers_from_form():
        """Parse trigger arrays from the submitted form."""
        types = request.form.getlist("trigger_type[]")
        days_list = request.form.getlist("trigger_days[]")
        confirm_days_list = request.form.getlist("trigger_confirm_days[]")
        confirm_emails = request.form.getlist("trigger_confirm_email[]")
        move_tos = request.form.getlist("trigger_move_to[]")

        triggers = []
        for i, ttype in enumerate(types):
            action = request.form.get(f"trigger_action_{i}", "delete")
            # Build confirm_methods from checkboxes
            methods = []
            if request.form.get(f"trigger_cm_unwatched_{i}"):
                methods.append("mark_unwatched")
            if request.form.get(f"trigger_cm_watching_{i}"):
                methods.append("start_watching")
            if request.form.get(f"trigger_cm_snooze_{i}"):
                methods.append("snooze")
            if request.form.get(f"trigger_cm_disable_{i}"):
                methods.append("disable")
            if not methods:
                methods = ["snooze"]

            triggers.append(Trigger(
                type=ttype,
                days=int(days_list[i]) if i < len(days_list) else 7,
                action=action,
                move_to=move_tos[i] if i < len(move_tos) and move_tos[i] else None,
                confirm_days=int(confirm_days_list[i]) if i < len(confirm_days_list) else 7,
                confirm_methods=",".join(methods),
                confirm_email=confirm_emails[i] if i < len(confirm_emails) and confirm_emails[i] else None,
                enabled=True,
            ))
        return triggers

    @app.route("/rules/new", methods=["GET", "POST"])
    @login_required
    def rules_new():
        if request.method == "POST":
            db = get_session()
            rule = Rule(
                scope=request.form["scope"],
                plex_library=request.form.get("plex_library") or None,
                plex_rating_key=request.form.get("plex_rating_key") or None,
                media_title=request.form.get("media_title") or None,
                action=request.form["action"],
                watched_by=",".join(request.form.getlist("watched_by")) or "any",
                protect_on_deck="protect_on_deck" in request.form,
                processing_mode=request.form.get("processing_mode", "episode"),
                min_episodes=int(request.form.get("min_episodes", 0)),
                remove_show_when_empty=request.form.get("remove_show_when_empty", "never"),
                enabled="enabled" in request.form,
                external_id=request.form.get("external_id") or None,
                external_source=request.form.get("external_source") or None,
            )
            rule.triggers = _parse_triggers_from_form()
            # Capture TVDB/TMDB IDs from Plex for durable identification
            if rule.plex_rating_key:
                try:
                    from mediapurge.clients import plex as plex_client
                    server = plex_client._server()
                    item = server.fetchItem(int(rule.plex_rating_key))
                    for guid in getattr(item, "guids", []):
                        if guid.id.startswith("tvdb://"):
                            rule.tvdb_id = int(guid.id[7:])
                        elif guid.id.startswith("tmdb://"):
                            rule.tmdb_id = int(guid.id[7:])
                except Exception:
                    pass
            db.add(rule)
            db.commit()
            db.close()
            next_url = request.form.get("next") or url_for("rules_list")
            return redirect(next_url)

        breadcrumb = None
        rating_key = request.args.get("plex_rating_key")
        if rating_key:
            try:
                from mediapurge.clients import plex as plex_client
                server = plex_client._server()
                item = server.fetchItem(int(rating_key))
                breadcrumb = {"title": item.title, "thumb": item.thumb,
                              "year": getattr(item, "year", ""), "type": item.type}
            except Exception:
                pass
        try:
            from mediapurge.clients import plex as plex_client
            plex_users = plex_client.get_users()
        except Exception:
            plex_users = []
        try:
            move_destinations = plex_client.get_move_destinations()
        except Exception:
            move_destinations = []
        return render_template("rule_form_v2.html", rule=None, breadcrumb=breadcrumb, plex_users=plex_users, move_destinations=move_destinations)

    @app.route("/rules/<int:rule_id>/edit", methods=["GET", "POST"])
    @login_required
    def rules_edit(rule_id):
        db = get_session()
        rule = db.execute(
            select(Rule).options(joinedload(Rule.triggers)).where(Rule.id == rule_id)
        ).unique().scalar_one_or_none()
        if not rule:
            db.close()
            return redirect(url_for("rules_list"))

        if request.method == "POST":
            rule.scope = request.form["scope"]
            rule.plex_library = request.form.get("plex_library") or None
            rule.plex_rating_key = request.form.get("plex_rating_key") or None
            rule.media_title = request.form.get("media_title") or None
            rule.action = request.form["action"]
            rule.watched_by = ",".join(request.form.getlist("watched_by")) or "any"
            rule.protect_on_deck = "protect_on_deck" in request.form
            rule.processing_mode = request.form.get("processing_mode", "episode")
            rule.min_episodes = int(request.form.get("min_episodes", 0))
            rule.remove_show_when_empty = request.form.get("remove_show_when_empty", "never")
            rule.enabled = "enabled" in request.form
            rule.external_id = request.form.get("external_id") or rule.external_id
            rule.external_source = request.form.get("external_source") or rule.external_source
            # Replace triggers
            rule.triggers.clear()
            rule.triggers = _parse_triggers_from_form()
            db.commit()
            db.close()
            return redirect(url_for("rules_list"))

        # Convert triggers to dicts for template
        triggers_data = []
        for t in rule.triggers:
            triggers_data.append({
                "type": t.type, "days": t.days, "action": t.action,
                "move_to": t.move_to,
                "confirm_days": t.confirm_days, "confirm_methods": t.confirm_methods,
                "confirm_email": t.confirm_email,
            })
        if not triggers_data:
            triggers_data = [{}]

        db.close()
        try:
            from mediapurge.clients import plex as plex_client
            plex_users = plex_client.get_users()
        except Exception:
            plex_users = []

        # Attach triggers data to rule object for template
        rule.triggers_data = triggers_data
        try:
            move_destinations = plex_client.get_move_destinations()
        except Exception:
            move_destinations = []
        return render_template("rule_form_v2.html", rule=rule, breadcrumb=None, plex_users=plex_users, move_destinations=move_destinations)

    @app.route("/rules/<int:rule_id>/delete", methods=["POST"])
    @login_required
    def rules_delete(rule_id):
        db = get_session()
        rule = db.get(Rule, rule_id)
        if rule:
            from mediapurge.models import PendingAction
            db.query(PendingAction).filter_by(rule_id=rule.id, confirmed=False, cancelled=False).update({"cancelled": True})
            db.delete(rule)
            db.commit()
        db.close()
        return redirect(url_for("rules_list"))

    _orphan_task = {"running": False, "results": None}
    import threading
    from mediapurge.engine import maintenance_lock
    _orphan_lock = threading.Lock()

    @app.route("/orphans")
    @login_required
    def orphans():
        with _orphan_lock:
            if _orphan_task["running"]:
                return render_template("orphans.html", orphans=None, running=True)
        if request.args.get("scan"):
            with _orphan_lock:
                _orphan_task["running"] = True
                _orphan_task["results"] = None
            def _scan():
                from mediapurge.engine import run_orphan_scan
                try:
                    sync_managed_media()
                    results = run_orphan_scan()
                except Exception:
                    results = []
                with _orphan_lock:
                    _orphan_task["results"] = results
                    _orphan_task["running"] = False
            threading.Thread(target=_scan, daemon=True).start()
            return render_template("orphans.html", orphans=None, running=True)
        with _orphan_lock:
            results = _orphan_task["results"]
        return render_template("orphans.html", orphans=results, running=False)

    @app.route("/orphans/status")
    @login_required
    def orphans_status():
        import json as jsonlib
        with _orphan_lock:
            if _orphan_task["running"]:
                return jsonlib.dumps({"running": True})
        return jsonlib.dumps({"running": False, "done": True})

    @app.route("/log")
    @login_required
    def action_log():
        db = get_session()
        logs = db.execute(
            select(ActionLog).order_by(desc(ActionLog.timestamp)).limit(100)
        ).scalars().all()
        db.close()
        return render_template("log.html", logs=logs)

    # Background task state
    _task_lock = threading.Lock()
    _task = {"running": False, "report": None, "error": None, "mode": None}

    def _run_task(dry_run):
        if not maintenance_lock.acquire(blocking=False):
            with _task_lock:
                _task["error"] = "Maintenance already running"
                _task["running"] = False
            return
        try:
            sync_managed_media()
            report = run_evaluation(dry_run=dry_run)
            if not dry_run:
                from mediapurge.engine import execute_deletions, execute_moves, process_pending_actions
                process_pending_actions()
                execute_deletions(report)
                execute_moves(report)
            with _task_lock:
                _task["report"] = report
        except Exception as e:
            with _task_lock:
                _task["error"] = str(e)
        finally:
            maintenance_lock.release()
        with _task_lock:
            _task["running"] = False

    @app.route("/preview")
    @login_required
    def preview():
        mode = request.args.get("mode")
        with _task_lock:
            if _task["running"]:
                return render_template("preview.html", report=None, error=None, ran=False, running=True)
            if _task["report"] is not None and mode is None:
                report = _task["report"]
                ran = _task["mode"] == "run"
                return render_template("preview.html", report=report, error=_task["error"], ran=ran, running=False)
        if mode in ("preview", "run"):
            with _task_lock:
                _task["running"] = True
                _task["report"] = None
                _task["error"] = None
                _task["mode"] = mode
            dry_run = mode == "preview"
            threading.Thread(target=_run_task, args=(dry_run,), daemon=True).start()
            return render_template("preview.html", report=None, error=None, ran=False, running=True)
        return render_template("preview.html", report=None, error=None, ran=False, running=False)

    @app.route("/preview/status")
    @login_required
    def preview_status():
        import json as jsonlib
        with _task_lock:
            if _task["running"]:
                return jsonlib.dumps({"running": True})
            if _task["error"]:
                return jsonlib.dumps({"running": False, "error": _task["error"]})
        return jsonlib.dumps({"running": False, "done": True})

    @app.route("/preview/results")
    @login_required
    def preview_results():
        with _task_lock:
            report = _task["report"]
            error = _task["error"]
            ran = _task["mode"] == "run"
        return render_template("preview.html", report=report, error=error, ran=ran, running=False)

    @app.route("/run", methods=["POST"])
    @login_required
    def run_now():
        with _task_lock:
            if not _task["running"]:
                _task["running"] = True
                _task["report"] = None
                _task["error"] = None
                _task["mode"] = "run"
                threading.Thread(target=_run_task, args=(False,), daemon=True).start()
        return render_template("preview.html", report=None, error=None, ran=False, running=True)

    @app.route("/browse")
    @login_required
    def browse():
        """List Plex libraries."""
        from mediapurge.clients import plex as plex_client
        libraries = [(name, ltype) for name, ltype in plex_client.get_libraries() if ltype in ("show", "movie")]
        db = get_session()
        lib_rules_map = {}
        for r in db.query(Rule).filter(Rule.scope == "library", Rule.enabled == True).all():
            lib_rules_map.setdefault(r.plex_library, []).append(r)
        db.close()
        return render_template("browse.html", libraries=libraries, items=None, item=None, children=None,
                               lib_rules_map=lib_rules_map)

    @app.route("/browse/<library>")
    @login_required
    def browse_library(library):
        """List items in a library."""
        from mediapurge.clients import plex as plex_client
        items = plex_client.get_library_items(library)
        mgr_info = plex_client.get_manager_info()
        items_data = []
        for i in items:
            viewed_at = getattr(i, "lastViewedAt", None)
            paths = plex_client.get_file_paths(i)
            mgr = None
            for p in paths:
                if p in mgr_info:
                    mgr = mgr_info[p]
                    break
            items_data.append({
                "title": i.title, "rating_key": i.ratingKey, "type": i.type,
                "year": getattr(i, "year", ""), "thumb": i.thumb,
                "viewed_at": viewed_at.strftime("%Y-%m-%d") if viewed_at else "Never",
                "managers": ", ".join(mgr["managers"]) if mgr else "—",
                "ended": mgr["ended"] if mgr else None,
            })
        db = get_session()
        rules_by_key = {r.plex_rating_key: r for r in db.query(Rule).filter(Rule.scope == "show", Rule.enabled == True).all()}
        lib_rules = db.query(Rule).filter(Rule.scope == "library", Rule.plex_library == library, Rule.enabled == True).all()
        db.close()
        return render_template("browse.html", libraries=None, items=items_data,
                               library=library, item=None, children=None,
                               rules_by_key=rules_by_key, lib_rules=lib_rules)

    @app.route("/browse/<library>/<int:rating_key>")
    @login_required
    def browse_item(library, rating_key):
        """Show detail for a specific item."""
        from mediapurge.clients import plex as plex_client
        server = plex_client._server()
        item = server.fetchItem(rating_key)
        children = []
        if item.type == "show":
            for season in item.seasons():
                for ep in season.episodes():
                    info = plex_client.get_last_viewed_info(ep)
                    children.append({
                        "title": f"S{ep.parentIndex:02d}E{ep.index:02d} - {ep.title}",
                        "rating_key": ep.ratingKey,
                        "watched": ep.isWatched,
                        "thumb": ep.thumb,
                        "viewed_at": info["viewed_at"].strftime("%Y-%m-%d") if info["viewed_at"] else "—",
                        "viewed_by": info["viewed_by"] or "—",
                    })
        item_data = {"title": item.title, "rating_key": item.ratingKey, "type": item.type,
                     "year": getattr(item, "year", ""), "thumb": item.thumb}
        try:
            move_destinations = plex_client.get_move_destinations()
        except Exception:
            move_destinations = []
        return render_template("browse.html", libraries=None, items=None,
                               library=library, item=item_data, children=children,
                               move_destinations=move_destinations)

    @app.route("/browse/<library>/<int:rating_key>/delete", methods=["POST"])
    @login_required
    def browse_delete(library, rating_key):
        """Immediately delete an item from its manager."""
        from mediapurge.clients import plex as plex_client
        from mediapurge.engine import find_manager, _delete_direct, EvalResult
        server = plex_client._server()
        item = server.fetchItem(rating_key)
        manager, manager_id = find_manager(item)
        result = EvalResult(title=item.title, rating_key=str(rating_key), action="delete",
                            manager=manager, manager_id=manager_id)
        try:
            if manager == "sonarr":
                sonarr.delete_series(int(manager_id), delete_files=True)
            elif manager == "radarr":
                radarr.delete_movie(int(manager_id), delete_files=True)
            elif manager == "medusa":
                medusa.delete_show(str(manager_id), remove_files=True)
            else:
                _delete_direct(result)
            db = get_session()
            db.add(ActionLog(media_title=item.title, plex_rating_key=str(rating_key),
                             action_taken="delete", details="immediate from browse"))
            # Purge any rules targeting this item
            for r in db.query(Rule).filter(Rule.plex_rating_key == str(rating_key)).all():
                db.delete(r)
            db.commit()
            db.close()
        except Exception as e:
            flash(f"Operation failed: {e}", "error")
        return redirect(url_for("browse_library", library=library))

    @app.route("/browse/<library>/<int:rating_key>/move", methods=["POST"])
    @login_required
    def browse_move(library, rating_key):
        """Immediately move an item to another location."""
        from mediapurge.clients import plex as plex_client
        from mediapurge.engine import find_manager, _do_move, EvalResult
        dest = request.form.get("move_to", "")
        if not dest:
            return redirect(url_for("browse_item", library=library, rating_key=rating_key))
        server = plex_client._server()
        item = server.fetchItem(rating_key)
        manager, manager_id = find_manager(item)
        result = EvalResult(title=item.title, rating_key=str(rating_key), action="move",
                            manager=manager, manager_id=manager_id, move_to=dest)
        try:
            _do_move(result, dest)
            db = get_session()
            db.add(ActionLog(media_title=item.title, plex_rating_key=str(rating_key),
                             action_taken="move", details=f"immediate move to {dest}"))
            db.commit()
            db.close()
        except Exception as e:
            flash(f"Operation failed: {e}", "error")
        return redirect(url_for("browse_library", library=library))

    @app.route("/browse/wanted")
    @login_required
    def browse_wanted():
        """Show wanted items from managers that don't have rules yet and aren't in Plex."""
        from mediapurge.clients import plex as plex_client
        db = get_session()
        # Get ALL existing rules to exclude items that already have rules
        all_rules = db.execute(select(Rule)).scalars().all()
        existing_external_ids = {r.external_id for r in all_rules if r.external_id}
        existing_titles = {r.media_title.lower() for r in all_rules if r.media_title}
        existing_plex_keys = {r.plex_rating_key for r in all_rules if r.plex_rating_key}
        # Build lookup: external_id or title -> rule_id and plex_rating_key
        rule_by_ext = {r.external_id: r.id for r in all_rules if r.external_id}
        rule_by_title = {r.media_title.lower(): r.id for r in all_rules if r.media_title}
        plex_key_by_ext = {r.external_id: r.plex_rating_key for r in all_rules if r.external_id and r.plex_rating_key}
        plex_key_by_title = {r.media_title.lower(): r.plex_rating_key for r in all_rules if r.media_title and r.plex_rating_key}
        db.close()

        # Get items in Plex for exclusion
        plex_guids = set()
        try:
            for lib_name, lib_type in plex_client.get_libraries():
                if lib_type not in ("show", "movie"):
                    continue
                for item in plex_client.get_library_items(lib_name):
                    for guid in getattr(item, "guids", []):
                        # Normalize to our format: "tvdb:123"
                        gid = guid.id
                        if "://" in gid:
                            source, value = gid.split("://", 1)
                            plex_guids.add(f"{source}:{value}")
        except Exception:
            pass

        wanted_items = []

        # Radarr: monitored movies without files
        try:
            for m in radarr.get_wanted_movies():
                tmdb_id = m.get("tmdbId")
                imdb_id = m.get("imdbId")
                ext_id = None
                ext_source = None
                if tmdb_id:
                    ext_id = f"tmdb:{tmdb_id}"
                    ext_source = "tmdb"
                elif imdb_id:
                    ext_id = f"imdb:{imdb_id}"
                    ext_source = "imdb"
                if ext_id and ext_id not in plex_guids:
                    rule_id = rule_by_ext.get(ext_id) or rule_by_title.get(m.get("title", "").lower())
                    wanted_items.append({
                        "title": m.get("title", "Unknown"),
                        "year": m.get("year", ""),
                        "manager": "Radarr",
                        "status": "Wanted",
                        "external_id": ext_id,
                        "external_source": ext_source,
                        "type": "movie",
                        "rule_id": rule_id,
                        "plex_key": plex_key_by_ext.get(ext_id) or plex_key_by_title.get(m.get("title", "").lower()),
                    })
        except Exception:
            pass

        # Sonarr: monitored series not in Plex
        try:
            for s in sonarr.get_wanted_series():
                tvdb_id = s.get("tvdbId")
                ext_id = f"tvdb:{tvdb_id}" if tvdb_id else None
                if ext_id and ext_id not in plex_guids:
                    rule_id = rule_by_ext.get(ext_id) or rule_by_title.get(s.get("title", "").lower())
                    wanted_items.append({
                        "title": s.get("title", "Unknown"),
                        "year": s.get("year", ""),
                        "manager": "Sonarr",
                        "status": "Monitored",
                        "external_id": ext_id,
                        "external_source": "tvdb",
                        "type": "show",
                        "rule_id": rule_id,
                        "plex_key": plex_key_by_ext.get(ext_id) or plex_key_by_title.get(s.get("title", "").lower()),
                    })
        except Exception:
            pass

        # Medusa: shows with wanted episodes not in Plex
        try:
            for s in medusa.get_wanted_shows():
                tvdb_id = s.get("id", {}).get("tvdb")
                ext_id = f"tvdb:{tvdb_id}" if tvdb_id else None
                if ext_id and ext_id not in plex_guids:
                    rule_id = rule_by_ext.get(ext_id) or rule_by_title.get(s.get("title", "").lower())
                    wanted_items.append({
                        "title": s.get("title", "Unknown"),
                        "year": s.get("year", ""),
                        "manager": "Medusa",
                        "status": "Wanted",
                        "external_id": ext_id,
                        "external_source": "tvdb",
                        "type": "show",
                        "rule_id": rule_id,
                        "plex_key": plex_key_by_ext.get(ext_id) or plex_key_by_title.get(s.get("title", "").lower()),
                    })
        except Exception:
            pass

        return render_template("wanted.html", items=wanted_items)

    @app.route("/immediate/delete/<int:rating_key>", methods=["POST"])
    @login_required
    def immediate_delete(rating_key):
        """Immediately delete an item."""
        from mediapurge.clients import plex as plex_client
        from mediapurge.engine import find_manager, _delete_direct, EvalResult
        from mediapurge.clients import sonarr, radarr, medusa
        import warnings; warnings.filterwarnings("ignore")
        server = plex_client._server()
        item = server.fetchItem(rating_key)
        paths = plex_client.get_file_paths(item)
        manager, manager_id = find_manager(item)
        result = EvalResult(title=item.title, rating_key=str(rating_key), action="delete",
                            manager=manager, manager_id=manager_id)
        try:
            # Delete from ALL managers that track this item
            deleted_files = False
            for path in paths:
                p = path.rstrip("/")
                # Check Sonarr
                for s in sonarr.get_all_series():
                    if s["path"].rstrip("/") == p:
                        sonarr.delete_series(s["id"], delete_files=not deleted_files)
                        deleted_files = True
                        break
                # Check Radarr
                for m in radarr.get_all_movies():
                    if m.get("path", "").rstrip("/") == p:
                        radarr.delete_movie(m["id"], delete_files=not deleted_files)
                        deleted_files = True
                        break
                # Check Medusa
                for s in medusa.get_all_shows():
                    if s.get("config", {}).get("location", "").rstrip("/") == p:
                        medusa.delete_show(s["id"]["slug"], remove_files=not deleted_files)
                        deleted_files = True
                        break
            if not deleted_files:
                _delete_direct(result)
            db = get_session()
            db.add(ActionLog(media_title=item.title, plex_rating_key=str(rating_key),
                             action_taken="delete", details="immediate"))
            # Purge any rules targeting this item
            for r in db.query(Rule).filter(Rule.plex_rating_key == str(rating_key)).all():
                db.delete(r)
            db.commit(); db.close()
            plex_client.scan_library(item.librarySectionTitle)
        except Exception as e:
            flash(f"Operation failed: {e}", "error")
        return redirect(url_for("rules_list"))

    @app.route("/immediate/move/<int:rating_key>", methods=["POST"])
    @login_required
    def immediate_move(rating_key):
        """Immediately move an item."""
        import time
        from mediapurge.clients import plex as plex_client
        from mediapurge.engine import find_manager, _do_move, EvalResult
        dest = request.form.get("move_to", "")
        if not dest:
            return redirect(url_for("rules_list"))
        server = plex_client._server()
        item = server.fetchItem(rating_key)
        # Capture watch status
        if hasattr(item, "episodes"):
            watch_status = {(ep.parentIndex, ep.index): ep.isWatched for ep in item.episodes()}
        else:
            watch_status = {"item": item.isWatched}

        manager, manager_id = find_manager(item)
        result = EvalResult(title=item.title, rating_key=str(rating_key), action="move",
                            manager=manager, manager_id=manager_id, move_to=dest)
        try:
            _do_move(result, dest)
            db = get_session()
            db.add(ActionLog(media_title=item.title, plex_rating_key=str(rating_key),
                             action_taken="move", details=f"immediate to {dest}"))
            # Purge any rules targeting this item (move = new location, old rule is stale)
            for r in db.query(Rule).filter(Rule.plex_rating_key == str(rating_key)).all():
                db.delete(r)
            db.commit(); db.close()
            plex_client.scan_library(item.librarySectionTitle)
            # Restore watch status
            time.sleep(10)
            for _ in range(3):
                found = None
                for section in server.library.sections():
                    try:
                        for i in section.search(item.title):
                            if i.title == item.title:
                                found = i
                                break
                    except Exception:
                        continue
                    if found:
                        break
                if found:
                    break
                time.sleep(5)
            if found:
                if hasattr(found, "episodes"):
                    for ep in found.episodes():
                        if watch_status.get((ep.parentIndex, ep.index)):
                            ep.markWatched()
                elif watch_status.get("item"):
                    found.markWatched()
        except Exception as e:
            flash(f"Operation failed: {e}", "error")
        return redirect(url_for("rules_list"))

    @app.route("/plex_thumb")
    @login_required
    def plex_thumb():

        """Proxy Plex thumbnails."""
        import requests as req
        thumb_path = request.args.get("path", "")
        if not thumb_path or not thumb_path.startswith("/") or "?" in thumb_path or ".." in thumb_path:
            return "", 404
        cfg_plex = get_config()["plex"]
        url = f"{cfg_plex['url']}{thumb_path}?X-Plex-Token={cfg_plex['token']}"
        r = req.get(url)
        return r.content, r.status_code, {"Content-Type": r.headers.get("Content-Type", "image/jpeg")}

    @app.route("/config", methods=["GET", "POST"])
    @login_required
    def config_edit():
        import yaml, re
        from mediapurge.config import get_config, load_config
        config_path = os.environ.get("MEDIACLEANER_CONFIG", "config.yaml")
        MASK = "••••••••"
        SENSITIVE_KEYS = ("smtp_pass", "admin_password", "secret_key", "api_key", "token")

        def _mask_yaml(text):
            def replacer(m):
                return f"{m.group(1)}{MASK}"
            for key in SENSITIVE_KEYS:
                text = re.sub(rf"({key}:\s*).+", replacer, text)
            return text

        def _restore_secrets(new_text, old_text):
            old_cfg = yaml.safe_load(old_text) or {}
            new_cfg = yaml.safe_load(new_text) or {}
            _restore_nested(new_cfg, old_cfg)
            return new_cfg

        def _restore_nested(new, old, path=""):
            if isinstance(new, dict) and isinstance(old, dict):
                for k, v in new.items():
                    if isinstance(v, dict):
                        _restore_nested(v, old.get(k, {}))
                    elif v == MASK and k in old:
                        new[k] = old[k]

        if request.method == "POST":
            try:
                with open(config_path) as f:
                    old_text = f.read()
                new_cfg = _restore_secrets(request.form["config_yaml"], old_text)
                if not isinstance(new_cfg, dict):
                    raise ValueError("Config must be a YAML mapping")
                with open(config_path, "w") as f:
                    yaml.dump(new_cfg, f, default_flow_style=False, sort_keys=False)
                load_config(config_path)
                return render_template("config.html", config_yaml=_mask_yaml(request.form["config_yaml"]),
                                       success="Configuration saved. Restart service for some changes to take effect.")
            except Exception as e:
                return render_template("config.html", config_yaml=request.form["config_yaml"],
                                       error=f"Invalid config: {e}")
        with open(config_path) as f:
            config_yaml = f.read()
        return render_template("config.html", config_yaml=_mask_yaml(config_yaml))

    @app.route("/config/password", methods=["POST"])
    @login_required
    def config_password():
        import yaml
        config_path = os.environ.get("MEDIACLEANER_CONFIG", "config.yaml")
        new_pass = request.form.get("new_password", "")
        if len(new_pass) < 4:
            return redirect(url_for("config_edit"))
        hashed = bcrypt.hashpw(new_pass.encode(), bcrypt.gensalt()).decode()
        with open(config_path) as f:
            raw = yaml.safe_load(f)
        raw["web"]["admin_password"] = hashed
        with open(config_path, "w") as f:
            yaml.dump(raw, f, default_flow_style=False, sort_keys=False)
        cfg["web"]["admin_password"] = hashed
        return redirect(url_for("config_edit"))

    @app.route("/config/test-email", methods=["POST"])
    @login_required
    def config_test_email():
        from mediapurge import notify
        try:
            notify.send("MediaPurge Test", "This is a test email from MediaPurge.")
            return redirect(url_for("config_edit") + "?msg=sent")
        except Exception as e:
            return redirect(url_for("config_edit") + f"?msg=fail&err={e}")

    @app.route("/config/test-connections", methods=["POST"])
    @login_required
    def config_test_connections():
        from mediapurge.clients import plex as plex_client, sonarr, radarr, medusa, ombi
        import json
        results = {}
        tests = {
            "plex": lambda: plex_client._server().friendlyName,
            "sonarr": lambda: len(sonarr.get_all_series()),
            "radarr": lambda: len(radarr.get_all_movies()),
            "medusa": lambda: len(medusa.get_all_shows()),
            "ombi": lambda: len(ombi.get_movie_requests()) + len(ombi.get_tv_requests()),
        }
        for name, fn in tests.items():
            try:
                result = fn()
                results[name] = {"ok": True, "detail": str(result)}
            except Exception as e:
                results[name] = {"ok": False, "detail": str(e)}
        return render_template("config.html", config_yaml=None, test_results=results)

    @app.route("/confirm/snooze/<token>")
    def confirm_snooze(token):
        from mediapurge.engine import cancel_pending_by_token
        if cancel_pending_by_token(token, "snooze"):
            return render_template("confirm.html", success=True)
        return render_template("confirm.html", success=False)

    @app.route("/confirm/disable/<token>")
    def confirm_disable(token):
        from mediapurge.engine import cancel_pending_by_token
        if cancel_pending_by_token(token, "disable"):
            return render_template("confirm.html", success=True)
        return render_template("confirm.html", success=False)

    @app.route("/confirm/unwatched/<token>")
    def confirm_unwatched(token):
        from mediapurge.engine import cancel_pending_by_token
        if cancel_pending_by_token(token, "unwatched"):
            return render_template("confirm.html", success=True)
        return render_template("confirm.html", success=False)

    @app.route("/confirm/keep/<token>")
    def confirm_keep(token):
        """Legacy URL — treat as snooze."""
        from mediapurge.engine import cancel_pending_by_token
        if cancel_pending_by_token(token, "snooze"):
            return render_template("confirm.html", success=True)
        return render_template("confirm.html", success=False)

    from mediapurge.scheduler import start_scheduler
    start_scheduler(app)

    return app


def main():
    app = create_app()
    cfg = get_config()
    ssl_ctx = None
    cert = cfg["web"].get("ssl_cert")
    key = cfg["web"].get("ssl_key")
    if cert and key:
        ssl_ctx = (cert, key)
    app.run(host="0.0.0.0", port=cfg["web"].get("port", 9393), ssl_context=ssl_ctx)


if __name__ == "__main__":
    main()
