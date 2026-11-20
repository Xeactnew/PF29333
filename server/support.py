"""Customer support: tickets on the website, emailed to the support address, answered from the admin."""
from __future__ import annotations

from . import config, mailer, notify, services
from .db import Ticket, TicketMessage, User, get_setting, utcnow


def support_address(s) -> str:
    return get_setting(s, "support_email") or ""


def _admin_link(t: Ticket) -> str:
    from . import runtime
    base = runtime.public_url()
    return f"{base}/admin/support/{t.id}" if base else f"Admin → Support → #{t.id}"


def create_ticket(s, user: User | None, subject: str, body: str, name: str = "", email: str = "") -> Ticket:
    t = Ticket(user_id=user.id if user else None, subject=subject[:200], name=(name or (user.username if user else ""))[:128],
               email=(email or (user.email if user else ""))[:255])
    s.add(t)
    s.flush()
    s.add(TicketMessage(ticket_id=t.id, author="customer", body=body))
    who = f"{user.username} ({user.plan.name})" if user else f"{t.name} (not logged in)"
    mailer.queue(s, support_address(s), f"[{config.SITE_NAME} support #{t.id}] {t.subject}",
                 f"From: {who}\nEmail: {t.email or '-'}\n\n{body}\n\n---\nReply on the website: {_admin_link(t)}\n"
                 f"(Replying to this email goes straight to the customer.)", reply_to=t.email)
    notify.send_admins(f"🛟 Support #{t.id} from {who}: {t.subject}\n\n{body[:500]}")
    services.log(s, "support_ticket", user.id if user else None, f"#{t.id} {t.subject}", user.username if user else t.name)
    return t


def reply(s, t: Ticket, author: str, body: str, from_admin: bool) -> TicketMessage:
    m = TicketMessage(ticket_id=t.id, author=author, body=body)
    s.add(m)
    t.updated_at = utcnow()
    if from_admin:
        t.status = "answered"
        mailer.queue(s, t.email, f"Re: {t.subject} [{config.SITE_NAME} support #{t.id}]",
                     f"Hi {t.name or 'there'},\n\n{body}\n\n---\nYou can also see and answer this on the Support page "
                     f"when you're logged in.\n{config.SITE_NAME}", reply_to=support_address(s))
    else:
        t.status = "open"
        mailer.queue(s, support_address(s), f"[{config.SITE_NAME} support #{t.id}] {t.subject} (new reply)",
                     f"{t.name} replied:\n\n{body}\n\n---\nReply on the website: {_admin_link(t)}", reply_to=t.email)
        notify.send_admins(f"🛟 Support #{t.id}, new reply from {t.name}:\n\n{body[:500]}")
    return m
