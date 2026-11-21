"""Database models (SQLite via SQLAlchemy)."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import (JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text,
                        UniqueConstraint, create_engine, event, select)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker

from . import config


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class Admin(Base):
    __tablename__ = "admins"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    telegram_chat_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_login: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Plan(Base):
    __tablename__ = "plans"
    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(32), unique=True)      # standard / pro / extreme
    name: Mapped[str] = mapped_column(String(64))
    monthly_price: Mapped[float] = mapped_column(Float, default=0)
    lifetime_price: Mapped[float] = mapped_column(Float, default=0)
    currency: Mapped[str] = mapped_column(String(8), default="£")
    features: Mapped[dict] = mapped_column(JSON, default=dict)
    sort: Mapped[int] = mapped_column(Integer, default=0)


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    email: Mapped[str] = mapped_column(String(255), default="")
    password_hash: Mapped[str] = mapped_column(String(255))
    session_version: Mapped[int] = mapped_column(Integer, default=1)
    plan_id: Mapped[int] = mapped_column(ForeignKey("plans.id"))
    billing: Mapped[str] = mapped_column(String(16), default="free")   # free / monthly / lifetime
    status: Mapped[str] = mapped_column(String(24), default="active")  # active / payment_check / closed
    paid_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    created_by: Mapped[str] = mapped_column(String(64), default="signup")
    last_login: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_seen: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    login_count: Mapped[int] = mapped_column(Integer, default=0)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # customer-linked Telegram (paid plans: extra channels)
    tg_session_enc: Mapped[str] = mapped_column(Text, default="")
    tg_phone: Mapped[str] = mapped_column(String(32), default="")
    tg_name: Mapped[str] = mapped_column(String(128), default="")
    # access time limit set by an admin (None = no limit); the account closes by itself when it passes
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    reminders_sent: Mapped[dict] = mapped_column(JSON, default=dict)
    # trade alerts through the Telegram bot (linked with a one-tap deep link from Plan & login)
    alert_chat_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    alert_prefs: Mapped[dict] = mapped_column(JSON, default=dict)
    alert_state: Mapped[dict] = mapped_column(JSON, default=dict)     # e.g. last daily summary date

    plan: Mapped[Plan] = relationship()
    accounts: Mapped[list["Account"]] = relationship(back_populates="user", cascade="all, delete-orphan")


class Node(Base):
    """A Windows VPS that runs MT5 terminals for customer accounts."""
    __tablename__ = "nodes"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    token_hash: Mapped[str] = mapped_column(String(128))
    max_slots: Mapped[int] = mapped_column(Integer, default=10)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_seen: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    info: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Account(Base):
    """A customer's MT5 account."""
    __tablename__ = "accounts"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    label: Mapped[str] = mapped_column(String(64), default="")
    login: Mapped[str] = mapped_column(String(32))
    server: Mapped[str] = mapped_column(String(128))
    password_enc: Mapped[str] = mapped_column(Text)
    copy_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    settings: Mapped[dict] = mapped_column(JSON, default=dict)
    node_id: Mapped[int | None] = mapped_column(ForeignKey("nodes.id"), nullable=True)
    waiting_since: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    removed: Mapped[bool] = mapped_column(Boolean, default=False)
    event_cursor: Mapped[int] = mapped_column(Integer, default=-1)   # -1 = start from latest
    # live state reported by the worker
    conn_status: Mapped[str] = mapped_column(String(24), default="new")  # new/waiting/starting/online/error/stopped
    conn_error: Mapped[str] = mapped_column(Text, default="")
    balance: Mapped[float] = mapped_column(Float, default=0)
    equity: Mapped[float] = mapped_column(Float, default=0)
    currency: Mapped[str] = mapped_column(String(8), default="")
    broker: Mapped[str] = mapped_column(String(128), default="")
    live_state: Mapped[dict] = mapped_column(JSON, default=dict)     # positions, pendings, blocks
    last_report: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    alert_state: Mapped[dict] = mapped_column(JSON, default=dict)     # what customer alerts already covered

    user: Mapped[User] = relationship(back_populates="accounts")
    node: Mapped[Node | None] = relationship()


class Source(Base):
    """Where signals come from: the PipsFarmer VIP group, a customer's channel, or manual entry."""
    __tablename__ = "sources"
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))      # main / channel / manual
    owner_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    tg_chat_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    title: Mapped[str] = mapped_column(String(128))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AccountSource(Base):
    __tablename__ = "account_sources"
    __table_args__ = (UniqueConstraint("account_id", "source_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"))
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"))


class Signal(Base):
    __tablename__ = "signals"
    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"))
    symbol: Mapped[str] = mapped_column(String(16), default="XAUUSD")
    side: Mapped[str] = mapped_column(String(4))               # buy / sell
    order_type: Mapped[str] = mapped_column(String(8), default="market")  # market / limit / stop
    entry_low: Mapped[float] = mapped_column(Float)
    entry_high: Mapped[float] = mapped_column(Float)
    sl: Mapped[float | None] = mapped_column(Float, nullable=True)
    tps: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(16), default="open")   # open / closed / cancelled
    tp_reached: Mapped[int] = mapped_column(Integer, default=0)
    raw_text: Mapped[str] = mapped_column(Text, default="")
    tg_msg_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tg_chat_id: Mapped[int | None] = mapped_column(Integer, nullable=True)   # which Telegram group it came from
    channel: Mapped[str] = mapped_column(String(128), default="")             # that group's name
    created_by: Mapped[str] = mapped_column(String(64), default="telegram")
    target_account_ids: Mapped[list] = mapped_column(JSON, default=list)  # manual signals for specific accounts
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    source: Mapped[Source] = relationship()


class SignalUpdate(Base):
    __tablename__ = "signal_updates"
    id: Mapped[int] = mapped_column(primary_key=True)
    signal_id: Mapped[int] = mapped_column(ForeignKey("signals.id"))
    kind: Mapped[str] = mapped_column(String(16))   # tp_hit / breakeven / close / close_partial / sl_hit / sl_move / cancel
    value: Mapped[float | None] = mapped_column(Float, nullable=True)
    raw_text: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(64), default="telegram")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Event(Base):
    """Queue read by MT5 workers. source_id events go to every account following that
    source; account_id events go to one account only."""
    __tablename__ = "events"
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))     # signal / update / command
    source_id: Mapped[int | None] = mapped_column(ForeignKey("sources.id"), nullable=True, index=True)
    account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id"), nullable=True, index=True)
    signal_id: Mapped[int | None] = mapped_column(ForeignKey("signals.id"), nullable=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Deal(Base):
    __tablename__ = "deals"
    __table_args__ = (UniqueConstraint("account_id", "ticket"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    ticket: Mapped[int] = mapped_column(Integer)
    position_id: Mapped[int] = mapped_column(Integer, default=0)
    time: Mapped[datetime] = mapped_column(DateTime, index=True)
    symbol: Mapped[str] = mapped_column(String(32), default="")
    side: Mapped[str] = mapped_column(String(8), default="")
    entry: Mapped[str] = mapped_column(String(8), default="")       # in / out / inout / balance
    volume: Mapped[float] = mapped_column(Float, default=0)
    price: Mapped[float] = mapped_column(Float, default=0)
    profit: Mapped[float] = mapped_column(Float, default=0)          # profit + swap + commission + fee
    ours: Mapped[bool] = mapped_column(Boolean, default=False)
    signal_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    comment: Mapped[str] = mapped_column(String(64), default="")


class Snapshot(Base):
    __tablename__ = "snapshots"
    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    time: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    balance: Mapped[float] = mapped_column(Float)
    equity: Mapped[float] = mapped_column(Float)


class Renewal(Base):
    __tablename__ = "renewals"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    due: Mapped[datetime] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(16), default="pending")   # pending / paid / not_paid
    asked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    tg_messages: Mapped[list] = mapped_column(JSON, default=list)        # [[chat_id, message_id], ...]
    answered_by: Mapped[str] = mapped_column(String(64), default="")
    answered_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    user: Mapped[User] = relationship()


class Activity(Base):
    __tablename__ = "activity"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    actor: Mapped[str] = mapped_column(String(64), default="")
    action: Mapped[str] = mapped_column(String(64))
    detail: Mapped[str] = mapped_column(Text, default="")
    ip: Mapped[str] = mapped_column(String(64), default="")
    time: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class NewsEvent(Base):
    __tablename__ = "news"
    id: Mapped[int] = mapped_column(primary_key=True)
    time: Mapped[datetime] = mapped_column(DateTime, index=True)
    currency: Mapped[str] = mapped_column(String(8))
    impact: Mapped[str] = mapped_column(String(16))
    title: Mapped[str] = mapped_column(String(255))
    __table_args__ = (UniqueConstraint("time", "currency", "title"),)


class UpgradeRequest(Base):
    """A customer asked for a paid plan; an admin marks it paid (activates) or declines."""
    __tablename__ = "upgrade_requests"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    plan_id: Mapped[int] = mapped_column(ForeignKey("plans.id"))
    billing: Mapped[str] = mapped_column(String(16), default="monthly")    # monthly / lifetime
    status: Mapped[str] = mapped_column(String(16), default="pending")     # pending / approved / declined
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    handled_by: Mapped[str] = mapped_column(String(64), default="")
    handled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    user: Mapped[User] = relationship()
    plan: Mapped[Plan] = relationship()


class Email(Base):
    """Outgoing email queue (payment reminders, support). Sent by the scheduler, retried if SMTP is down."""
    __tablename__ = "emails"
    id: Mapped[int] = mapped_column(primary_key=True)
    to: Mapped[str] = mapped_column(String(255))
    subject: Mapped[str] = mapped_column(String(255))
    body: Mapped[str] = mapped_column(Text)
    reply_to: Mapped[str] = mapped_column(String(255), default="")
    status: Mapped[str] = mapped_column(String(16), default="queued")      # queued / sent / failed
    tries: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Ticket(Base):
    """A support request from a customer (or from the public help form)."""
    __tablename__ = "tickets"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    name: Mapped[str] = mapped_column(String(128), default="")
    email: Mapped[str] = mapped_column(String(255), default="")
    subject: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(16), default="open")         # open / answered / closed
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    user: Mapped[User | None] = relationship()
    messages: Mapped[list["TicketMessage"]] = relationship(back_populates="ticket", order_by="TicketMessage.id",
                                                           cascade="all, delete-orphan")


class TicketMessage(Base):
    __tablename__ = "ticket_messages"
    id: Mapped[int] = mapped_column(primary_key=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id"))
    author: Mapped[str] = mapped_column(String(64))      # "customer" or the admin's username
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    ticket: Mapped[Ticket] = relationship(back_populates="messages")


class CustomerAlert(Base):
    """A trade alert for a customer's Telegram. `key` is unique, so the same alert is never queued twice;
    the bot sends queued ones and retries failures with a growing wait."""
    __tablename__ = "customer_alerts"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    chat_id: Mapped[int] = mapped_column(Integer)
    key: Mapped[str] = mapped_column(String(128), unique=True)
    kind: Mapped[str] = mapped_column(String(16))                    # open / close / manage / problems / daily / test
    text: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)  # queued / sent / failed / dropped
    tries: Mapped[int] = mapped_column(Integer, default=0)
    next_try: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ArchivedCredential(Base):
    """A former customer's connection details, kept apart from the live records so they survive even a
    full delete. Matched back by username or email when the same person returns. The MT5 side (login,
    server, encrypted password) lets a returning customer reconnect without re-entering anything. The
    Telegram session is kept here ONLY while the person hasn't withdrawn it: it is wiped when they unlink
    Telegram or are deleted, because it reaches their whole personal Telegram, not just the signal groups."""
    __tablename__ = "archived_credentials"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), index=True)
    username_key: Mapped[str] = mapped_column(String(64), unique=True)     # lower-cased, for matching
    email_key: Mapped[str] = mapped_column(String(255), default="", index=True)
    mt5: Mapped[list] = mapped_column(JSON, default=list)       # [{login, server, broker, label, password_enc, settings, sources}]
    tg_session_enc: Mapped[str] = mapped_column(Text, default="")
    tg_phone: Mapped[str] = mapped_column(String(32), default="")
    tg_name: Mapped[str] = mapped_column(String(128), default="")
    channels: Mapped[list] = mapped_column(JSON, default=list)   # [{tg_chat_id, title}] their own channels
    reason: Mapped[str] = mapped_column(String(32), default="")  # left / deleted
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON)


# ---------------------------------------------------------------- engine

_engine = None
SessionLocal = sessionmaker(expire_on_commit=False)


def init_engine(db_path: str | None = None):
    global _engine
    path = db_path or config.DB_PATH
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    url = "sqlite://" if path == ":memory:" else f"sqlite:///{path}"
    kwargs = {"connect_args": {"check_same_thread": False, "timeout": 30}}
    if path == ":memory:":
        from sqlalchemy.pool import StaticPool
        kwargs["poolclass"] = StaticPool
    _engine = create_engine(url, **kwargs)

    @event.listens_for(_engine, "connect")
    def _pragmas(dbapi_conn, _):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.close()

    SessionLocal.configure(bind=_engine)
    Base.metadata.create_all(_engine)
    _add_missing_columns(_engine)
    with SessionLocal() as s:
        seed(s)
    return _engine


def _add_missing_columns(engine) -> None:
    """Updates may add columns to existing tables; SQLite needs them added by hand."""
    from sqlalchemy import inspect, text
    insp = inspect(engine)
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            have = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in have:
                    continue
                coltype = col.type.compile(dialect=engine.dialect)
                default = ""
                if col.default is not None and getattr(col.default, "is_scalar", False):
                    v = col.default.arg
                    default = f" DEFAULT {int(v) if isinstance(v, bool) else repr(v) if isinstance(v, str) else v}"
                elif col.nullable is False:
                    if "JSON" in coltype:
                        default = " DEFAULT '{}'"
                    else:
                        default = " DEFAULT ''" if "CHAR" in coltype or "TEXT" in coltype else " DEFAULT 0"
                conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {coltype}{default}'))


OLD_CONTACT_TEXT = "To upgrade, message us on Telegram and pay directly. We'll switch your plan straight away."
DEFAULT_SETTINGS = {
    "gh_update_mode": "auto",    # new commits on GitHub main install by themselves (the owner OKs each push with Claude)
    # house rules per VIP group (Telegram chat id). Fx by KD: "risk free" / "move SL to <price>" =
    # each trade's own entry + 15 pips, so late entries are protected the same way. Editable in Admin → Telegram.
    # KD + FX_BY_H: tp_target and wait_for_zone come from the 90-day replay of their calls (Nov 2026):
    # TP1 for everything, and KD's quick "BUY GOLD!" waits for his zone instead of buying at market.
    "optimise_requests": {},     # chat -> {title, users, at, status: requested/not_possible, note}
    "group_rules": {"-1004498117492": {"be_lock_pips": 15.0, "sl_move_as_be": True, "tp_target": "tp1",
                                       "wait_for_zone": True},
                    "-1004407977092": {"tp_target": "tp1"}},
    "allow_signup": True,
    "contact_text": "We take crypto or bank transfer. Message @xaupipsfarmer on Telegram to pay and we'll "
                    "switch your plan on straight away.",
    "contact_link": "https://t.me/xaupipsfarmer",
    "capacity_alert_free_slots": 2,
    "support_email": "bhatiiraza06@gmail.com",
    "reminder_emails": True,
    "reminder_days": "3,1",
}


def seed(s) -> None:
    from .plans import DEFAULT_PLANS
    for p in DEFAULT_PLANS:
        plan = s.scalar(select(Plan).where(Plan.key == p["key"]))
        if not plan:
            s.add(Plan(**p))
        elif any(k not in (plan.features or {}) for k in p["features"]):
            # a feature added in a later version: the plan gets its default, the admin's own values stay
            plan.features = {**p["features"], **(plan.features or {})}
    if not s.scalar(select(Source).where(Source.kind == "main")):
        s.add(Source(kind="main", title=f"{config.SITE_NAME} VIP"))
    for k, v in DEFAULT_SETTINGS.items():
        if not s.get(Setting, k):
            s.add(Setting(key=k, value={"v": v}))
    s.flush()
    if not s.get(Setting, "defaults_v13"):          # one-off: payment details for installs from before 1.3
        if get_setting(s, "contact_link") in ("", None):
            set_setting(s, "contact_link", DEFAULT_SETTINGS["contact_link"])
        if get_setting(s, "contact_text") == OLD_CONTACT_TEXT:
            set_setting(s, "contact_text", DEFAULT_SETTINGS["contact_text"])
        s.add(Setting(key="defaults_v13", value={"v": True}))
    s.commit()


def get_setting(s, key: str):
    row = s.get(Setting, key)
    return row.value.get("v") if row else DEFAULT_SETTINGS.get(key)


def set_setting(s, key: str, value) -> None:
    row = s.get(Setting, key)
    if row:
        row.value = {"v": value}
    else:
        s.add(Setting(key=key, value={"v": value}))


def main_source(s) -> Source:
    return s.scalar(select(Source).where(Source.kind == "main"))
