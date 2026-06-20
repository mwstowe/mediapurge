"""Notification dispatch — email or Discord webhook."""

import logging
import smtplib
from email.message import EmailMessage

import requests

from mediapurge.config import get_config

log = logging.getLogger(__name__)


def _default_recipient() -> str:
    """Get admin email from config."""
    cfg = get_config().get("notifications", {}).get("email", {})
    return cfg.get("admin", "")


def send(subject: str, body: str):
    cfg = get_config().get("notifications", {})
    if not cfg.get("enabled"):
        return

    method = cfg.get("method", "email")
    if method == "email":
        recipient = _default_recipient()
        if recipient:
            _send_email(subject, body, cfg.get("email", {}), recipient)
    elif method == "discord":
        _send_discord(subject, body, cfg["discord"])


def send_to(subject: str, body: str, recipient: str):
    """Send to a specific email recipient (for confirmation emails)."""
    cfg = get_config().get("notifications", {})
    _send_email(subject, body, cfg.get("email", {}), recipient)


def _send_email(subject: str, body: str, cfg: dict, recipient: str):
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg.get("from", cfg.get("smtp_user", "mediapurge@localhost"))
    msg["To"] = recipient
    msg.set_content(body)

    try:
        with smtplib.SMTP(cfg["smtp_host"], cfg["smtp_port"]) as s:
            if cfg.get("smtp_port") == 587:
                s.starttls()
            if cfg.get("smtp_user"):
                s.login(cfg["smtp_user"], cfg["smtp_pass"])
            s.send_message(msg)
    except Exception as e:
        log.error(f"Failed to send email: {e}")


def _send_discord(subject: str, body: str, cfg: dict):
    content = f"**{subject}**\n```\n{body[:1900]}\n```"
    try:
        requests.post(cfg["webhook_url"], json={"content": content})
    except Exception as e:
        log.error(f"Failed to send Discord notification: {e}")
