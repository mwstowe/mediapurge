from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from mediapurge.db import Base


def _utcnow():
    return datetime.now(timezone.utc)


class Rule(Base):
    __tablename__ = "rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scope: Mapped[str] = mapped_column(String)  # library/show/season/episode
    plex_library: Mapped[str | None] = mapped_column(String, nullable=True)
    plex_rating_key: Mapped[str | None] = mapped_column(String, nullable=True)
    media_title: Mapped[str | None] = mapped_column(String, nullable=True)
    action: Mapped[str] = mapped_column(String, default='delete')  # keep/delete/move
    move_to: Mapped[str | None] = mapped_column(String, nullable=True)  # destination path for move
    watched_by: Mapped[str] = mapped_column(String, default='any')
    protect_on_deck: Mapped[bool] = mapped_column(Boolean, default=True)
    processing_mode: Mapped[str] = mapped_column(String, default='episode')  # episode/season
    min_episodes: Mapped[int] = mapped_column(Integer, default=0)
    remove_show_when_empty: Mapped[str] = mapped_column(String, default="if_ended")  # never/always/if_ended
    snoozed_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)

    triggers: Mapped[list["Trigger"]] = relationship("Trigger", backref="rule", cascade="all, delete-orphan")


class Trigger(Base):
    __tablename__ = "triggers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    rule_id: Mapped[int] = mapped_column(Integer, ForeignKey('rules.id'))
    type: Mapped[str] = mapped_column(String)  # watched/inactive/age
    days: Mapped[int] = mapped_column(Integer, default=7)
    action: Mapped[str] = mapped_column(String, default='delete')  # delete/confirm
    confirm_days: Mapped[int] = mapped_column(Integer, default=7)
    confirm_methods: Mapped[str] = mapped_column(String, default='snooze')  # comma-separated
    confirm_email: Mapped[str | None] = mapped_column(String, nullable=True)
    snoozed_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class PendingAction(Base):
    """Tracks items awaiting user confirmation before deletion."""
    __tablename__ = "pending_actions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    rule_id: Mapped[int] = mapped_column(Integer, ForeignKey("rules.id"))
    trigger_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("triggers.id"), nullable=True)
    plex_rating_key: Mapped[str] = mapped_column(String)
    media_title: Mapped[str] = mapped_column(String)
    token: Mapped[str] = mapped_column(String, unique=True)
    confirm_method: Mapped[str] = mapped_column(String)
    notified_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    notified_to: Mapped[str | None] = mapped_column(String, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    cancelled: Mapped[bool] = mapped_column(Boolean, default=False)


class ActionLog(Base):
    __tablename__ = "action_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    media_title: Mapped[str] = mapped_column(String)
    plex_rating_key: Mapped[str | None] = mapped_column(String, nullable=True)
    rule_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("rules.id"), nullable=True)
    action_taken: Mapped[str] = mapped_column(String)
    dry_run: Mapped[bool] = mapped_column(Boolean, default=False)
    details: Mapped[str | None] = mapped_column(Text, nullable=True)
    confirmed: Mapped[bool] = mapped_column(Boolean, default=False)


class ManagedMedia(Base):
    __tablename__ = "managed_media"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    plex_rating_key: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    title: Mapped[str] = mapped_column(String)
    plex_library: Mapped[str | None] = mapped_column(String, nullable=True)
    manager: Mapped[str] = mapped_column(String)
    manager_id: Mapped[str | None] = mapped_column(String, nullable=True)
    file_path: Mapped[str | None] = mapped_column(String, nullable=True)
    last_synced: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
