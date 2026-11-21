"""Archived customer logins, kept apart from the live records.

When a customer leaves (their plan is cancelled or their time runs out) or is deleted, a snapshot of
their connection details is written to the `archived_credentials` table. This survives even a full
delete, so if the same person signs up again (matched by username or email) their MT5 accounts come
back without re-entering anything.

Two rules protect the customer:
  * The Telegram session reaches their whole personal Telegram, not just the signal groups, so it is
    kept here only while they haven't withdrawn it. Unlinking Telegram, or being deleted, wipes it.
  * Nothing here is ever shown; it is used only to rebuild the customer's own accounts when they return.

Passwords stay encrypted the whole way through: the archive stores the same encrypted blob the live
account held, so this module never sees a plain password or decrypts a Telegram session.
"""
from __future__ import annotations

import re

from sqlalchemy import select

from . import services
from .db import Account, AccountSource, ArchivedCredential, Source, User, utcnow


def _key(text: str) -> str:
    return re.sub(r"\s+", "", (text or "").strip().lower())


def _find(s, username: str = "", email: str = "") -> ArchivedCredential | None:
    row = None
    if username:
        row = s.scalar(select(ArchivedCredential).where(ArchivedCredential.username_key == _key(username)))
    if not row and email:
        row = s.scalar(select(ArchivedCredential).where(ArchivedCredential.email_key == _key(email)))
    return row


def _mt5_rows(s, user: User) -> list[dict]:
    """Every MT5 account the customer still has a password for, with the groups it copied."""
    out = []
    for a in s.scalars(select(Account).where(Account.user_id == user.id, Account.removed.is_(False))):
        if not a.password_enc:
            continue
        out.append({"login": a.login, "server": a.server, "broker": a.broker, "label": a.label,
                    "password_enc": a.password_enc, "settings": a.settings or {},
                    "sources": services.account_source_ids(s, a.id)})
    return out


def _channels(s, user: User) -> list[dict]:
    return [{"tg_chat_id": c.tg_chat_id, "title": c.title} for c in
            s.scalars(select(Source).where(Source.kind == "channel", Source.owner_user_id == user.id))]


def archive(s, user: User, reason: str, keep_telegram: bool = True) -> ArchivedCredential:
    """Snapshot a customer's logins. reason: "left" (cancelled / expired) or "deleted".
    keep_telegram=False drops the Telegram session (used when they've withdrawn it)."""
    row = _find(s, user.username, user.email) or ArchivedCredential(username_key=_key(user.username))
    row.username = user.username
    row.username_key = _key(user.username)
    row.email_key = _key(user.email)
    mt5 = _mt5_rows(s, user)
    if mt5:                                  # never wipe a good snapshot with an empty one
        row.mt5 = mt5
    row.channels = _channels(s, user) or row.channels
    if keep_telegram and user.tg_session_enc:
        row.tg_session_enc = user.tg_session_enc
        row.tg_phone, row.tg_name = user.tg_phone, user.tg_name
    elif not keep_telegram:
        row.tg_session_enc = row.tg_phone = row.tg_name = ""
    row.reason = reason
    row.updated_at = utcnow()
    if row.id is None:
        s.add(row)
    return row


def forget_telegram(s, username: str = "", email: str = "") -> None:
    """Wipe the archived Telegram session (on unlink): the customer withdrew our access to their Telegram."""
    row = _find(s, username, email)
    if row:
        row.tg_session_enc = row.tg_phone = row.tg_name = ""
        row.updated_at = utcnow()


def has_archive(s, username: str = "", email: str = "") -> ArchivedCredential | None:
    return _find(s, username, email)


def restore(s, user: User, actor: str = "") -> dict:
    """Bring a returning customer's accounts back from the archive. Rebuilds any MT5 account they don't
    already have, and the Telegram link if the archive still holds one. Returns a short summary."""
    row = _find(s, user.username, user.email)
    if not row:
        return {"restored": False}
    from .plans import feature
    live = [a for a in s.scalars(select(Account).where(Account.user_id == user.id, Account.removed.is_(False)))]
    have = {(a.login, a.server) for a in s.scalars(select(Account).where(Account.user_id == user.id))}
    room = int(feature(user.plan.features, "max_accounts") or 1) - len(live)
    added = 0
    for m in row.mt5 or []:
        if room - added <= 0:                 # don't reconnect more than their current plan allows
            break
        if (m["login"], m["server"]) in have or not m.get("password_enc"):
            continue
        acc = Account(user_id=user.id, login=m["login"], server=m["server"], broker=m.get("broker", ""),
                      label=m.get("label", ""), password_enc=m["password_enc"],
                      settings=m.get("settings") or {}, conn_status="new")
        s.add(acc)
        s.flush()
        services.ensure_default_sources(s, acc)
        # re-link only the channels that still exist for this user (rebuilt below); main is already linked
        want = set(m.get("sources") or [])
        for sid in want:
            if s.get(Source, sid) and not s.scalar(select(AccountSource).where(
                    AccountSource.account_id == acc.id, AccountSource.source_id == sid)):
                s.add(AccountSource(account_id=acc.id, source_id=sid))
        added += 1
    tg = False
    if row.tg_session_enc and not user.tg_session_enc:
        user.tg_session_enc = row.tg_session_enc
        user.tg_phone, user.tg_name = row.tg_phone, row.tg_name
        for ch in row.channels or []:
            if not s.scalar(select(Source).where(Source.kind == "channel", Source.owner_user_id == user.id,
                                                 Source.tg_chat_id == ch["tg_chat_id"])):
                s.add(Source(kind="channel", owner_user_id=user.id, tg_chat_id=ch["tg_chat_id"],
                             title=ch["title"], active=False))
        tg = True
    if added or tg:
        services.log(s, "archive_restored", user.id, f"{added} MT5 account(s)" + (", Telegram" if tg else ""),
                     actor or user.username)
    return {"restored": bool(added or tg), "mt5": added, "telegram": tg}


def purge(s, username: str = "", email: str = "") -> None:
    """Remove the archive entirely (customer asked us to forget everything)."""
    row = _find(s, username, email)
    if row:
        s.delete(row)
