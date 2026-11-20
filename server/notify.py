"""Outgoing admin notifications. Anything can queue a message; the Telegram bot task
sends them. Messages are also kept for the admin page."""
from __future__ import annotations

import threading
from collections import deque
from datetime import datetime

_lock = threading.Lock()
_outbox: deque = deque()
recent: deque = deque(maxlen=50)
_wake = threading.Event()


def send_admins(text: str, buttons: list[list[tuple[str, str]]] | None = None,
                renewal_id: int | None = None, file: tuple[str, bytes] | None = None,
                chats: list[int] | None = None) -> None:
    """buttons: rows of (label, callback_data). file: (filename, bytes) sends a document.
    chats: only these chat ids (default: every linked admin)."""
    with _lock:
        _outbox.append({"text": text, "buttons": buttons, "renewal_id": renewal_id, "file": file, "chats": chats})
        if not file:
            recent.appendleft((datetime.now(), text))


def pending() -> int:
    with _lock:
        return len(_outbox)


def take_all() -> list[dict]:
    with _lock:
        items = list(_outbox)
        _outbox.clear()
    return items


def users_changed() -> None:
    """Something changed that may free or need a VPS slot; run assignment soon."""
    _wake.set()


def wait_changed(timeout: float) -> bool:
    hit = _wake.wait(timeout)
    _wake.clear()
    return hit
