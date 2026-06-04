import functools
import os

import bcrypt
from flask import Flask, redirect, render_template, request, session, url_for

from mediacleaner.config import get_config, load_config
from mediacleaner.db import get_session, init_db
from mediacleaner.engine import run_evaluation, sync_managed_media
from mediacleaner.models import ActionLog, ManagedMedia, Rule

from sqlalchemy import select, desc


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
        orphan_count = db.query(ManagedMedia).filter_by(manager="none").count()
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
        return render_template("rules.html", rules=rules)

    @app.route("/rules/new", methods=["GET", "POST"])
    @login_required
    def rules_new():
        if request.method == "POST":
            db = get_session()
            db.add(Rule(
                scope=request.form["scope"],
                plex_library=request.form.get("plex_library") or None,
                plex_rating_key=request.form.get("plex_rating_key") or None,
                media_title=request.form.get("media_title") or None,
                action=request.form["action"],
                min_days_watched=int(request.form.get("min_days_watched", 7)),
                max_days_age=int(request.form.get("max_days_age", 0)),
                max_days_inactive=int(request.form.get("max_days_inactive", 0)),
                min_episodes=int(request.form.get("min_episodes", 0)),
                watched_by=",".join(request.form.getlist("watched_by")) or "any",
                protect_on_deck="protect_on_deck" in request.form,
                all_watched="all_watched" in request.form,
                confirm_before_delete="confirm_before_delete" in request.form,
                confirm_days=int(request.form.get("confirm_days", 7)),
                confirm_method=request.form.get("confirm_method") or None,
                confirm_email=request.form.get("confirm_email") or None,
                enabled="enabled" in request.form,
            ))
            db.commit()
            db.close()
            return redirect(url_for("rules_list"))
        # Build breadcrumb if coming from browse
        breadcrumb = None
        rating_key = request.args.get("plex_rating_key")
        if rating_key:
            try:
                from mediacleaner.clients import plex as plex_client
                server = plex_client._server()
                item = server.fetchItem(int(rating_key))
                breadcrumb = {"title": item.title, "thumb": item.thumb,
                              "year": getattr(item, "year", "")}
            except Exception:
                pass
        # Get Plex users for dropdowns
        try:
            from mediacleaner.clients import plex as plex_client
            plex_users = plex_client.get_users()
        except Exception:
            plex_users = []
        return render_template("rule_form.html", rule=None, breadcrumb=breadcrumb, plex_users=plex_users)

    @app.route("/rules/<int:rule_id>/edit", methods=["GET", "POST"])
    @login_required
    def rules_edit(rule_id):
        db = get_session()
        rule = db.get(Rule, rule_id)
        if request.method == "POST":
            rule.scope = request.form["scope"]
            rule.plex_library = request.form.get("plex_library") or None
            rule.plex_rating_key = request.form.get("plex_rating_key") or None
            rule.media_title = request.form.get("media_title") or None
            rule.action = request.form["action"]
            rule.min_days_watched = int(request.form.get("min_days_watched", 7))
            rule.max_days_age = int(request.form.get("max_days_age", 0))
            rule.max_days_inactive = int(request.form.get("max_days_inactive", 0))
            rule.min_episodes = int(request.form.get("min_episodes", 0))
            rule.watched_by = ",".join(request.form.getlist("watched_by")) or "any"
            rule.protect_on_deck = "protect_on_deck" in request.form
            rule.all_watched = "all_watched" in request.form
            rule.confirm_before_delete = "confirm_before_delete" in request.form
            rule.confirm_days = int(request.form.get("confirm_days", 7))
            rule.confirm_method = request.form.get("confirm_method") or None
            rule.confirm_email = request.form.get("confirm_email") or None
            rule.enabled = "enabled" in request.form
            db.commit()
            db.close()
            return redirect(url_for("rules_list"))
        db.close()
        try:
            from mediacleaner.clients import plex as plex_client
            plex_users = plex_client.get_users()
        except Exception:
            plex_users = []
        return render_template("rule_form.html", rule=rule, breadcrumb=None, plex_users=plex_users)

    @app.route("/rules/<int:rule_id>/delete", methods=["POST"])
    @login_required
    def rules_delete(rule_id):
        db = get_session()
        rule = db.get(Rule, rule_id)
        if rule:
            db.delete(rule)
            db.commit()
        db.close()
        return redirect(url_for("rules_list"))

    @app.route("/orphans")
    @login_required
    def orphans():
        from mediacleaner.engine import run_orphan_scan
        try:
            sync_managed_media()
            orphan_list = run_orphan_scan()
        except Exception as e:
            orphan_list = []
        return render_template("orphans.html", orphans=orphan_list)

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
    _task = {"running": False, "report": None, "error": None, "mode": None}

    def _run_task(dry_run):
        try:
            sync_managed_media()
            report = run_evaluation(dry_run=dry_run)
            if not dry_run:
                from mediacleaner.engine import execute_deletions, process_pending_actions
                process_pending_actions()
                execute_deletions(report)
            _task["report"] = report
        except Exception as e:
            _task["error"] = str(e)
        _task["running"] = False

    @app.route("/preview")
    @login_required
    def preview():
        mode = request.args.get("mode")
        # If task is running, show the polling page regardless
        if _task["running"]:
            return render_template("preview.html", report=None, error=None, ran=False, running=True)
        # If task just finished and no new mode requested, show results
        if _task["report"] is not None and mode is None:
            report = _task["report"]
            ran = _task["mode"] == "run"
            return render_template("preview.html", report=report, error=_task["error"], ran=ran, running=False)
        # Start a new task
        if mode in ("preview", "run"):
            _task["running"] = True
            _task["report"] = None
            _task["error"] = None
            _task["mode"] = mode
            import threading
            dry_run = mode == "preview"
            threading.Thread(target=_run_task, args=(dry_run,), daemon=True).start()
            return render_template("preview.html", report=None, error=None, ran=False, running=True)
        return render_template("preview.html", report=None, error=None, ran=False, running=False)

    @app.route("/preview/status")
    @login_required
    def preview_status():
        import json as jsonlib
        if _task["running"]:
            return jsonlib.dumps({"running": True})
        if _task["error"]:
            return jsonlib.dumps({"running": False, "error": _task["error"]})
        return jsonlib.dumps({"running": False, "done": True})

    @app.route("/preview/results")
    @login_required
    def preview_results():
        report = _task["report"]
        error = _task["error"]
        ran = _task["mode"] == "run"
        return render_template("preview.html", report=report, error=error, ran=ran, running=False)

    @app.route("/run", methods=["POST"])
    @login_required
    def run_now():
        if not _task["running"]:
            _task["running"] = True
            _task["report"] = None
            _task["error"] = None
            _task["mode"] = "run"
            import threading
            threading.Thread(target=_run_task, args=(False,), daemon=True).start()
        return render_template("preview.html", report=None, error=None, ran=False, running=True)

    @app.route("/browse")
    @login_required
    def browse():
        """List Plex libraries."""
        from mediacleaner.clients import plex as plex_client
        libraries = plex_client.get_libraries()
        return render_template("browse.html", libraries=libraries, items=None, item=None, children=None)

    @app.route("/browse/<library>")
    @login_required
    def browse_library(library):
        """List items in a library."""
        from mediacleaner.clients import plex as plex_client
        items = plex_client.get_library_items(library)
        mgr_info = plex_client.get_manager_info()
        items_data = []
        for i in items:
            info = plex_client.get_last_viewed_info(i)
            paths = plex_client.get_file_paths(i)
            mgr = None
            for p in paths:
                if p in mgr_info:
                    mgr = mgr_info[p]
                    break
            items_data.append({
                "title": i.title, "rating_key": i.ratingKey, "type": i.type,
                "year": getattr(i, "year", ""), "thumb": i.thumb,
                "viewed_at": info["viewed_at"].strftime("%Y-%m-%d") if info["viewed_at"] else "Never",
                "viewed_by": info["viewed_by"] or "—",
                "managers": ", ".join(mgr["managers"]) if mgr else "—",
                "ended": mgr["ended"] if mgr else None,
            })
        return render_template("browse.html", libraries=None, items=items_data,
                               library=library, item=None, children=None)

    @app.route("/browse/<library>/<int:rating_key>")
    @login_required
    def browse_item(library, rating_key):
        """Show detail for a specific item (show seasons/episodes)."""
        from mediacleaner.clients import plex as plex_client
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
        return render_template("browse.html", libraries=None, items=None,
                               library=library, item=item_data, children=children)

    @app.route("/plex_thumb")
    @login_required
    def plex_thumb():
        """Proxy Plex thumbnails to avoid exposing the token to the browser."""
        import requests as req
        thumb_path = request.args.get("path", "")
        if not thumb_path:
            return "", 404
        cfg_plex = get_config()["plex"]
        url = f"{cfg_plex['url']}{thumb_path}?X-Plex-Token={cfg_plex['token']}"
        r = req.get(url)
        return r.content, r.status_code, {"Content-Type": r.headers.get("Content-Type", "image/jpeg")}

    @app.route("/config", methods=["GET", "POST"])
    @login_required
    def config_edit():
        import yaml, re
        from mediacleaner.config import get_config, load_config
        config_path = os.environ.get("MEDIACLEANER_CONFIG", "config.yaml")
        MASK = "••••••••"
        SENSITIVE_KEYS = ("smtp_pass", "admin_password", "secret_key", "api_key", "token")

        def _mask_yaml(text):
            """Mask sensitive values for display."""
            def replacer(m):
                return f"{m.group(1)}{MASK}"
            for key in SENSITIVE_KEYS:
                text = re.sub(rf"({key}:\s*).+", replacer, text)
            return text

        def _restore_secrets(new_text, old_text):
            """Restore masked values from the original file."""
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
        """Change admin password."""
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
        from mediacleaner import notify
        try:
            notify.send("MediaCleaner Test", "This is a test email from MediaCleaner.")
            return redirect(url_for("config_edit") + "?msg=sent")
        except Exception as e:
            return redirect(url_for("config_edit") + f"?msg=fail&err={e}")

    @app.route("/config/test-connections", methods=["POST"])
    @login_required
    def config_test_connections():
        from mediacleaner.clients import plex as plex_client, sonarr, radarr, medusa, ombi
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

    @app.route("/confirm/keep/<token>")
    def confirm_keep(token):
        """Public URL — no auth required. User clicks to cancel a pending deletion."""
        from mediacleaner.engine import cancel_pending_by_token
        if cancel_pending_by_token(token):
            return render_template("confirm.html", success=True)
        return render_template("confirm.html", success=False)

    from mediacleaner.scheduler import start_scheduler
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
