"""Notification dispatch — email or Discord webhook."""

import logging
import smtplib
from email.message import EmailMessage

import requests

from mediacleaner.config import get_config

log = logging.getLogger(__name__)


def send(subject: str, body: str):
    cfg = get_config().get("notifications", {})
    if not cfg.get("enabled"):
        return

    method = cfg.get("method", "email")
    if method == "email":
        _send_email(subject, body, cfg["email"])
    elif method == "discord":
        _send_discord(subject, body, cfg["discord"])


def send_to(subject: str, body: str, recipient: str):
    """Send to a specific email recipient (for confirmation emails)."""
    cfg = get_config().get("notifications", {})
    email_cfg = dict(cfg.get("email", {}))
    email_cfg["recipient"] = recipient
    _send_email(subject, body, email_cfg)


def _send_email(subject: str, body: str, cfg: dict):
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg.get("smtp_user", "mediacleaner@localhost")
    msg["To"] = cfg["recipient"]
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
