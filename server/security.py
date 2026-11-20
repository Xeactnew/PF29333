"""Password hashing, secret encryption, tokens."""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from collections import defaultdict

from cryptography.fernet import Fernet, InvalidToken

from . import config

# ------------------------------------------------------------ passwords (scrypt)

_N, _R, _P = 2 ** 14, 8, 1


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return f"scrypt${_N}${_R}${_P}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt, dk = stored.split("$")
        if algo != "scrypt":
            return False
        calc = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r),
                              p=int(p), dklen=32)
        return hmac.compare_digest(calc, base64.b64decode(dk))
    except (ValueError, TypeError):
        return False


def password_problem(password: str) -> str | None:
    if len(password) < 8:
        return "Password must be at least 8 characters."
    return None


# ------------------------------------------------------------ secrets at rest

def _fernet() -> Fernet:
    if not config.FERNET_KEY:
        raise RuntimeError("PF_FERNET_KEY is not set")
    return Fernet(config.FERNET_KEY.encode())


def encrypt(text: str) -> str:
    return _fernet().encrypt(text.encode()).decode() if text else ""


def decrypt(token: str) -> str:
    if not token:
        return ""
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken:
        return ""


# ------------------------------------------------------------ node tokens

def new_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_password(length: int = 12) -> str:
    alphabet = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(length))


# ------------------------------------------------------------ login rate limit

_attempts: dict[str, list[float]] = defaultdict(list)


def too_many_attempts(key: str, limit: int = 8, window: int = 600) -> bool:
    now = time.time()
    _attempts[key] = [t for t in _attempts[key] if now - t < window]
    return len(_attempts[key]) >= limit


def record_attempt(key: str) -> None:
    _attempts[key].append(time.time())


def clear_attempts(key: str) -> None:
    _attempts.pop(key, None)
