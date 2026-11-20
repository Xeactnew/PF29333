"""Paid plans without a payment provider: customers request a plan, pay by crypto or bank transfer
through Telegram, an admin marks it paid. Plus access time limits and payment reminder emails."""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select

from . import config, mailer, notify, services
from .db import Plan, SessionLocal, UpgradeRequest, User, get_setting, utcnow

TIME_LIMITS = [("none", "No time limit"), ("7", "7 days"), ("14", "14 days"), ("30", "30 days"),
               ("60", "60 days"), ("90", "90 days"), ("180", "6 months"), ("365", "1 year"), ("date", "Until a date")]


def limit_from_form(form: dict, now: datetime | None = None) -> tuple[bool, datetime | None]:
    """Returns (changed, expires_at). 'keep' (or missing) leaves the current limit alone."""
    now = now or utcnow()
    choice = str(form.get("time_limit", "keep"))
    if choice == "keep":
        return False, None
    if choice == "none":
        return True, None
    if choice == "date":
        try:
            return True, datetime.strptime(str(form.get("expires_on", "")), "%Y-%m-%d").replace(hour=23, minute=59)
        except ValueError:
            return False, None
    if choice.isdigit():
        return True, now + timedelta(days=int(choice))
    return False, None


def pay_text(s) -> tuple[str, str]:
    return (get_setting(s, "contact_text") or "", get_setting(s, "contact_link") or "")


# ------------------------------------------------------------ plan requests ("sign up for Pro")

def create_request(s, user: User, plan: Plan, billing: str) -> UpgradeRequest:
    for old in s.scalars(select(UpgradeRequest).where(UpgradeRequest.user_id == user.id,
                                                      UpgradeRequest.status == "pending")):
        old.status, old.handled_by, old.handled_at = "replaced", "customer", utcnow()
    r = UpgradeRequest(user_id=user.id, plan_id=plan.id, billing=billing)
    s.add(r)
    s.flush()
    price = plan.monthly_price if billing == "monthly" else plan.lifetime_price
    services.log(s, "plan_requested", user.id, f"{plan.name} {billing} (PF-{r.id})", user.username)
    notify.send_admins(
        f"🆕 Plan request PF-{r.id}: {user.username} wants {plan.name} {billing} "
        f"({plan.currency}{price:g}{'/month' if billing == 'monthly' else ' once'}).\n"
        f"They've been told to message @xaupipsfarmer to pay. Tap Paid once the money's in.",
        [[("✅ Paid, switch them on", f"ur:{r.id}:ok"), ("❌ Decline", f"ur:{r.id}:no")]])
    return r


def apply_request(s, r: UpgradeRequest, approve: bool, actor: str) -> str:
    if r.status != "pending":
        return f"PF-{r.id} was already {r.status}."
    r.status, r.handled_by, r.handled_at = ("approved" if approve else "declined"), actor, utcnow()
    user = r.user
    if not approve:
        services.log(s, "plan_request_declined", user.id, f"PF-{r.id}", actor)
        return f"PF-{r.id} declined ({user.username})."
    paid_until = services.add_months(utcnow()) if r.billing == "monthly" else None
    if user.status == "closed":
        services.reopen_user(s, user, actor)
    services.set_plan(s, user, r.plan, r.billing, paid_until, actor)
    services.assign_accounts(s)
    mailer.queue(s, user.email, f"You're on {config.SITE_NAME} {r.plan.name}",
                 f"Hi {user.username},\n\nThanks for your payment. Your account is now on {r.plan.name} "
                 f"({'monthly, paid until ' + paid_until.strftime('%d %b %Y') if paid_until else 'lifetime'}).\n\n"
                 f"Log in to set up your accounts and settings.\n\n{config.SITE_NAME}")
    return f"✅ PF-{r.id}: {user.username} is now on {r.plan.name} ({r.billing})."


def answer_request(request_id: int, approve: bool, actor: str) -> str:
    with SessionLocal() as s:
        r = s.get(UpgradeRequest, request_id)
        if not r:
            return "Request not found."
        text = apply_request(s, r, approve, actor)
        s.commit()
        return text


# ------------------------------------------------------------ time limits + reminders

def _reminder_body(s, user: User, what: str, when: datetime) -> str:
    text, link = pay_text(s)
    return (f"Hi {user.username},\n\n"
            f"Your {config.SITE_NAME} {user.plan.name} {what} on {when:%A %d %B %Y}.\n\n"
            f"To keep copying without a break: {text}\n"
            f"{('Telegram: ' + link) if link else ''}\n\n"
            f"If you've already paid, you can ignore this email.\n\n{config.SITE_NAME}")


def send_reminder_now(s, user: User) -> bool:
    if not user.email:
        return False
    if user.billing == "monthly" and user.paid_until:
        body = _reminder_body(s, user, "renews", user.paid_until)
    elif user.expires_at:
        body = _reminder_body(s, user, "access ends", user.expires_at)
    else:
        text, link = pay_text(s)
        body = f"Hi {user.username},\n\n{text}\n{('Telegram: ' + link) if link else ''}\n\n{config.SITE_NAME}"
    mailer.queue(s, user.email, f"{config.SITE_NAME}: payment reminder", body)
    return True


def billing_tick(now: datetime | None = None) -> None:
    """Closes accounts whose time limit has passed and emails payment reminders."""
    now = now or utcnow()
    with SessionLocal() as s:
        # time limits
        for user in s.scalars(select(User).where(User.expires_at.is_not(None), User.expires_at <= now,
                                                 User.status != "closed")):
            services.close_user(s, user, "system", "time limit reached")
            notify.send_admins(f"⌛ {user.username}'s time limit ended ({user.expires_at:%d %b %Y}), so their "
                               f"account is closed and copying stopped. Reopen them in Admin → Customers.")
            text, link = pay_text(s)
            mailer.queue(s, user.email, f"Your {config.SITE_NAME} access has ended",
                         f"Hi {user.username},\n\nYour {config.SITE_NAME} access ended today, so copying has stopped. "
                         f"Your open trades were left as they are.\n\nTo continue: {text}\n"
                         f"{('Telegram: ' + link) if link else ''}\n\n{config.SITE_NAME}")
        # reminders before a monthly renewal or a time limit
        if get_setting(s, "reminder_emails") is not False:
            days = sorted({int(d) for d in str(get_setting(s, "reminder_days") or "3,1").split(",")
                           if d.strip().isdigit()}, reverse=True)
            for user in s.scalars(select(User).where(User.status != "closed", User.email != "")):
                sent = dict(user.reminders_sent or {})
                checks = []
                if user.billing == "monthly" and user.paid_until:
                    checks.append(("pay", user.paid_until, "renews"))
                if user.expires_at:
                    checks.append(("exp", user.expires_at, "access ends"))
                for kind, when, what in checks:
                    left = (when.date() - now.date()).days
                    due = [d for d in days if 0 <= left <= d]          # thresholds reached so far
                    if not due:
                        continue
                    key = f"{kind}-{when:%Y%m%d}-{min(due)}"           # the most urgent one
                    if key in sent:
                        continue
                    for d in due:                                       # one email, even if several are due
                        sent[f"{kind}-{when:%Y%m%d}-{d}"] = now.isoformat()
                    when_txt = "today" if left == 0 else f"in {left} day{'s' if left > 1 else ''}"
                    mailer.queue(s, user.email, f"{config.SITE_NAME}: your plan {what} {when_txt}",
                                 _reminder_body(s, user, what, when))
                if sent != (user.reminders_sent or {}):
                    user.reminders_sent = sent
        s.commit()
