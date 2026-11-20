"""Shared web helpers: templates, sessions, CSRF, flash messages, auth."""
from __future__ import annotations

import json
import logging
import secrets
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from . import config
from .db import Admin, SessionLocal, User, utcnow
from .plans import FEATURES, feature

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


class Redirect(Exception):
    def __init__(self, url: str):
        self.url = url


def db_session():
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def client_ip(request: Request) -> str:
    return (request.headers.get("cf-connecting-ip") or request.headers.get("x-forwarded-for", "").split(",")[0]
            or (request.client.host if request.client else ""))[:64]


# ------------------------------------------------------------ csrf + flash

def csrf_token(request: Request) -> str:
    tok = request.session.get("csrf")
    if not tok:
        tok = secrets.token_urlsafe(24)
        request.session["csrf"] = tok
    return tok


class FormExpired(HTTPException):
    """The form's security token doesn't match this browser's session (old page, or cookies blocked)."""

    def __init__(self, cookie_missing: bool):
        self.cookie_missing = cookie_missing
        super().__init__(400, "This page had expired. Refresh it and try again." if not cookie_missing else
                         "Your browser didn't keep our login cookie. Allow cookies for this site "
                         "(private browsing or a content blocker can stop them), then try again.")


def form_expired_message(e: "FormExpired") -> str:
    if e.cookie_missing:
        return ("Your browser didn't keep our login cookie, so we couldn't log you in. If you're in a private "
                "window or using a content blocker, allow cookies for this site, or try another browser.")
    return "That page had expired, so we've refreshed it. Please try again."


async def form_data(request: Request) -> dict:
    form = await request.form()
    if not secrets.compare_digest(str(form.get("csrf", "")), request.session.get("csrf", "") or "x"):
        cookie_missing = "pf_session" not in request.cookies
        logging.getLogger("pf.web").warning(
            "form token mismatch on %s (cookie %s, session token %s, browser %s)", request.url.path,
            "missing" if cookie_missing else "present", "set" if request.session.get("csrf") else "none",
            (request.headers.get("user-agent") or "?")[:120])
        raise FormExpired(cookie_missing)
    data: dict = {}
    for k, v in form.multi_items():
        if k in data:
            data[k] = data[k] if isinstance(data[k], list) else [data[k]]
            data[k].append(v)
        else:
            data[k] = v
    return data


def flash(request: Request, text: str, kind: str = "ok") -> None:
    request.session.setdefault("flash", []).append([kind, text])


def pop_flash(request: Request) -> list:
    return request.session.pop("flash", [])


def redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


# ------------------------------------------------------------ auth

def current_user(request: Request, s) -> User:
    uid, sv = request.session.get("uid"), request.session.get("sv")
    user = s.get(User, uid) if uid else None
    if not user or user.session_version != sv:
        request.session.pop("uid", None)
        raise Redirect("/login")
    if user.status == "closed":
        request.session.pop("uid", None)
        raise Redirect("/login?closed=1")
    now = utcnow()
    if user.last_seen and now - user.last_seen > timedelta(minutes=30):
        # back after a break: the overview sums up what happened while they were away
        request.session["away_from"] = user.last_seen.isoformat()
        request.session["away_to"] = now.isoformat()
    if not user.last_seen or now - user.last_seen > timedelta(minutes=2):
        user.last_seen = now
        s.commit()
    return user


def current_admin(request: Request, s) -> Admin:
    aid = request.session.get("aid")
    admin = s.get(Admin, aid) if aid else None
    if not admin:
        raise Redirect("/admin/login")
    return admin


# ------------------------------------------------------------ template filters

CURRENCY_SIGNS = {"USD": "$", "GBP": "£", "EUR": "€", "USC": "¢", "JPY": "¥", "AUD": "A$", "CAD": "C$"}


def money(v, cur: str = "") -> str:
    cur = CURRENCY_SIGNS.get(cur, cur + " " if cur and len(cur) > 1 else cur)
    try:
        v = float(v or 0)
    except (TypeError, ValueError):
        return str(v)
    sign = "-" if v < 0 else ""
    return f"{sign}{cur}{abs(v):,.2f}"


def signed(v, cur: str = "") -> str:
    v = float(v or 0)
    return ("+" if v > 0 else "") + money(v, cur)


def ago(dt: datetime | None) -> str:
    if not dt:
        return "never"
    secs = (utcnow() - dt).total_seconds()
    if secs < 0:
        return "in " + until(dt)
    if secs < 90:
        return "just now"
    if secs < 3600:
        return f"{int(secs // 60)} min ago"
    if secs < 86400:
        return f"{int(secs // 3600)} h ago"
    days = int(secs // 86400)
    return "yesterday" if days == 1 else f"{days} days ago"


def until(dt: datetime | None) -> str:
    if not dt:
        return ""
    secs = (dt - utcnow()).total_seconds()
    if secs <= 0:
        return "now"
    if secs < 3600:
        return f"{int(secs // 60)} min"
    if secs < 86400:
        return f"{int(secs // 3600)} h"
    return f"{int(secs // 86400)} days"


def date(dt: datetime | None, fmt: str = "%d %b %Y") -> str:
    return dt.strftime(fmt) if dt else "—"


def price(v) -> str:
    if v is None:
        return "—"
    return f"{v:.2f}".rstrip("0").rstrip(".")


def duration(td) -> str:
    """4h 2m / 35 min / 2 days 3h"""
    secs = int(td.total_seconds()) if hasattr(td, "total_seconds") else int(td or 0)
    d, h, m = secs // 86400, secs % 86400 // 3600, secs % 3600 // 60
    if d:
        return f"{d} day{'s' if d != 1 else ''} {h}h" if h else f"{d} day{'s' if d != 1 else ''}"
    return f"{h}h {m}m" if h else f"{m} min"


templates.env.filters.update(duration=duration)


def _brand_logo(size=26):
    from markupsafe import Markup
    from .brand import logo_svg
    return Markup(logo_svg(size))


templates.env.globals.update(brand_logo=_brand_logo)
templates.env.filters.update(money=money, signed=signed, ago=ago, until=until, date=date, price=price,
                             tojson_safe=lambda v: json.dumps(v))
from . import site_icon  # noqa: E402

def _asset_version() -> str:
    """A fingerprint of the CSS/JS, added to their links so browsers and Cloudflare never use an old copy."""
    import hashlib
    h = hashlib.sha256()
    for name in ("style.css", "app.js"):
        try:
            h.update((Path(__file__).parent / "static" / name).read_bytes())
        except OSError:
            pass
    return h.hexdigest()[:10]


ASSET_V = _asset_version()

TAB_ICONS = {'home': '<path d="M4 11.5 12 5l8 6.5V19a1 1 0 0 1-1 1h-4.5v-5h-5v5H5a1 1 0 0 1-1-1z"/>', 'accounts': '<rect x="3.5" y="5" width="17" height="14" rx="2.5"/><path d="M3.5 9.5h17M7.5 14.5h4"/>', 'signals': '<path d="M4 15.5 9 10l4 4 7-8"/><path d="M15 6h5v5"/>', 'channels': '<path d="M5 12a7 7 0 0 1 14 0M8.5 12a3.5 3.5 0 0 1 7 0"/><circle cx="12" cy="12" r="1.3"/><path d="M12 13.5V20"/>', 'plan': '<circle cx="12" cy="8.5" r="3.5"/><path d="M5 19.5c1.2-3.3 3.8-5 7-5s5.8 1.7 7 5"/>', 'support': '<circle cx="12" cy="12" r="8.5"/><path d="M9.6 9.6a2.5 2.5 0 1 1 3.3 2.4c-.6.2-.9.7-.9 1.3v.4"/><circle cx="12" cy="16.6" r=".6"/>', 'customers': '<circle cx="9" cy="9" r="3"/><path d="M3.5 18.5c.9-2.6 3-4 5.5-4s4.6 1.4 5.5 4"/><circle cx="17" cy="9.5" r="2.4"/><path d="M15.5 14.7c2.3.1 4 1.3 4.8 3.8"/>', 'more': '<circle cx="6" cy="12" r="1.5"/><circle cx="12" cy="12" r="1.5"/><circle cx="18" cy="12" r="1.5"/>'}

templates.env.globals.update(site=config.SITE_NAME, FEATURES=FEATURES, feature=feature,
                             icon_tags=site_icon.head_tags, tab_icons=TAB_ICONS,
                             asset_v=ASSET_V)


def render(request: Request, name: str, ctx: dict | None = None, status: int = 200):
    ctx = dict(ctx or {})
    ctx.setdefault("flashes", pop_flash(request))
    ctx["csrf"] = csrf_token(request)
    ctx["path"] = request.url.path
    return templates.TemplateResponse(request, name, ctx, status_code=status)
