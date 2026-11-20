"""Email: a queue in the database, sent through your own SMTP account (for example Gmail with an
app password), set up in Admin → Settings → Email. If SMTP isn't set up or is down, emails wait
in the queue and go out once it works."""
from __future__ import annotations

import logging
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

from sqlalchemy import select

from . import config, security
from .db import Email, SessionLocal, get_setting, utcnow

log = logging.getLogger("pf.mail")
EMAIL_RE = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"


def smtp_settings(s) -> dict:
    return {"host": get_setting(s, "smtp_host") or "", "port": int(get_setting(s, "smtp_port") or 587),
            "user": get_setting(s, "smtp_user") or "", "password": security.decrypt(get_setting(s, "smtp_password_enc") or ""),
            "from_name": get_setting(s, "smtp_from_name") or config.SITE_NAME}


def configured(s) -> bool:
    c = smtp_settings(s)
    return bool(c["host"] and c["user"] and c["password"])


def queue(s, to: str, subject: str, body: str, reply_to: str = "") -> Email | None:
    """Adds an email to the queue (call s.commit() after). Skips empty addresses."""
    to = (to or "").strip()
    if not to or "@" not in to:
        return None
    e = Email(to=to[:255], subject=subject[:255], body=body, reply_to=(reply_to or "")[:255])
    s.add(e)
    return e


def _send_one(c: dict, e: Email) -> None:
    msg = EmailMessage()
    msg["From"] = formataddr((c["from_name"], c["user"]))
    msg["To"] = e.to
    msg["Subject"] = e.subject
    msg["Message-ID"] = make_msgid(domain=c["user"].split("@")[-1])
    if e.reply_to:
        msg["Reply-To"] = e.reply_to
    msg.set_content(e.body)
    ctx = ssl.create_default_context()
    if c["port"] == 465:
        with smtplib.SMTP_SSL(c["host"], c["port"], context=ctx, timeout=30) as smtp:
            smtp.login(c["user"], c["password"])
            smtp.send_message(msg)
    else:
        with smtplib.SMTP(c["host"], c["port"], timeout=30) as smtp:
            smtp.starttls(context=ctx)
            smtp.login(c["user"], c["password"])
            smtp.send_message(msg)


def send_pending(limit: int = 20, sender=_send_one) -> int:
    """Sends queued emails. Returns how many went out."""
    with SessionLocal() as s:
        if not configured(s):
            return 0
        c = smtp_settings(s)
        sent = 0
        for e in s.scalars(select(Email).where(Email.status == "queued").order_by(Email.id).limit(limit)):
            try:
                sender(c, e)
                e.status, e.sent_at = "sent", utcnow()
                sent += 1
            except Exception as ex:  # noqa: BLE001
                e.tries += 1
                e.last_error = str(ex)[:500]
                if e.tries >= 8:
                    e.status = "failed"
                log.warning("email %s to %s failed: %s", e.id, e.to, ex)
                if isinstance(ex, (smtplib.SMTPAuthenticationError, OSError)):
                    s.commit()
                    break               # SMTP itself is the problem; try again next round
            s.commit()
        return sent


def send_test(s, to: str) -> None:
    """Sends straight away (not queued) so the admin sees any error."""
    c = smtp_settings(s)
    if not configured(s):
        raise RuntimeError("Fill in the SMTP server, username and password first.")
    _send_one(c, Email(to=to, subject=f"{config.SITE_NAME} test email",
                       body="It works. Payment reminders and support emails will be sent from this account."))
