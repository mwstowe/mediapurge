from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from mediacleaner.config import get_config


class Base(DeclarativeBase):
    pass


_engine = None
_SessionLocal = None


def get_engine():
    global _engine
    if _engine is None:
        cfg = get_config()
        db_path = cfg.get("database", {}).get("path", "mediacleaner.db")
        _engine = create_engine(f"sqlite:///{db_path}", echo=False)
    return _engine


def get_session() -> Session:
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(bind=get_engine())
    return _SessionLocal()


def init_db():
    from mediacleaner import models  # noqa: F401

    Base.metadata.create_all(get_engine())
    _migrate()


def _migrate():
    """Add missing columns to existing tables without dropping data."""
    import sqlite3
    cfg = get_config()
    db_path = cfg.get("database", {}).get("path", "mediacleaner.db")
    conn = sqlite3.connect(db_path)

    def _add_col(table, col, coltype, default=None):
        existing = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
        if col not in existing:
            defstr = f" DEFAULT {default}" if default is not None else ""
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}{defstr}")

    _add_col("rules", "delete_by_season", "BOOLEAN", 0)
    _add_col("rules", "snoozed_until", "DATETIME", "NULL")
    _add_col("rules", "all_watched", "BOOLEAN", 0)
    _add_col("rules", "confirm_before_delete", "BOOLEAN", 1)
    _add_col("rules", "confirm_days", "INTEGER", 7)
    _add_col("rules", "confirm_method", "VARCHAR", "NULL")
    _add_col("rules", "confirm_email", "VARCHAR", "NULL")
    _add_col("rules", "max_days_inactive", "INTEGER", 0)

    conn.commit()
    conn.close()
