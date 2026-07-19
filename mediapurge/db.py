from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from mediapurge.config import get_config


class Base(DeclarativeBase):
    pass


_engine = None
_SessionLocal = None


def get_engine():
    global _engine
    if _engine is None:
        cfg = get_config()
        db_path = cfg.get("database", {}).get("path", "mediapurge.db")
        _engine = create_engine(f"sqlite:///{db_path}", echo=False,
                                connect_args={"timeout": 30})
    return _engine


def get_session() -> Session:
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(bind=get_engine())
    return _SessionLocal()


def init_db():
    from mediapurge import models  # noqa: F401

    Base.metadata.create_all(get_engine())
    _migrate()


def _migrate():
    """Add missing columns/tables to existing database."""
    import sqlite3
    cfg = get_config()
    db_path = cfg.get("database", {}).get("path", "mediapurge.db")
    conn = sqlite3.connect(db_path)

    def _add_col(table, col, coltype, default=None):
        existing = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
        if col not in existing:
            defstr = f" DEFAULT {default}" if default is not None else ""
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}{defstr}")

    # Ensure triggers table exists
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    if "triggers" not in tables:
        conn.execute("""CREATE TABLE triggers (
            id INTEGER PRIMARY KEY,
            rule_id INTEGER REFERENCES rules(id),
            type VARCHAR NOT NULL,
            days INTEGER DEFAULT 7,
            action VARCHAR DEFAULT 'delete',
            confirm_days INTEGER DEFAULT 7,
            confirm_methods VARCHAR DEFAULT 'snooze',
            confirm_email VARCHAR,
            snoozed_until DATETIME,
            enabled BOOLEAN DEFAULT 1
        )""")

    # Rules table migrations
    _add_col("rules", "processing_mode", "VARCHAR", "'episode'")
    _add_col("rules", "remove_show_when_empty", "VARCHAR", "'if_ended'")
    _add_col("rules", "snoozed_until", "DATETIME", "NULL")
    _add_col("rules", "move_to", "VARCHAR", "NULL")

    # PendingAction migrations
    _add_col("pending_actions", "trigger_id", "INTEGER", "NULL")
    _add_col("pending_actions", "notified_to", "VARCHAR", "NULL")

    # Trigger migrations
    _add_col("triggers", "move_to", "VARCHAR", "NULL")

    # Migrate Rule.action: delete/move → manage, push move_to to triggers
    existing_rules = [r[1] for r in conn.execute("PRAGMA table_info(rules)").fetchall()]
    if "move_to" in existing_rules:
        # Migrate move rules: set trigger.move_to from rule.move_to
        rows = conn.execute("SELECT id, move_to FROM rules WHERE action='move' AND move_to IS NOT NULL").fetchall()
        for rule_id, move_to in rows:
            conn.execute("UPDATE triggers SET move_to=?, action='move' WHERE rule_id=?", (move_to, rule_id))
        # Convert delete/move actions to manage
        conn.execute("UPDATE rules SET action='manage' WHERE action IN ('delete', 'move')")

    conn.commit()
    conn.close()
