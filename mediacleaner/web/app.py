import functools

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
                watched_by=request.form.get("watched_by", "any"),
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
        return render_template("rule_form.html", rule=None)

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
            rule.watched_by = request.form.get("watched_by", "any")
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
        return render_template("rule_form.html", rule=rule)

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
        db = get_session()
        items = db.execute(
            select(ManagedMedia).where(ManagedMedia.manager == "none")
        ).scalars().all()
        db.close()
        return render_template("orphans.html", orphans=items)

    @app.route("/log")
    @login_required
    def action_log():
        db = get_session()
        logs = db.execute(
            select(ActionLog).order_by(desc(ActionLog.timestamp)).limit(100)
        ).scalars().all()
        db.close()
        return render_template("log.html", logs=logs)

    @app.route("/preview")
    @login_required
    def preview():
        try:
            sync_managed_media()
            report = run_evaluation(dry_run=True)
        except Exception as e:
            return render_template("preview.html", report=None, error=str(e))
        return render_template("preview.html", report=report, error=None)

    @app.route("/confirm/keep/<token>")
    def confirm_keep(token):
        """Public URL — no auth required. User clicks to cancel a pending deletion."""
        from mediacleaner.engine import cancel_pending_by_token
        if cancel_pending_by_token(token):
            return render_template("confirm.html", success=True)
        return render_template("confirm.html", success=False)

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
