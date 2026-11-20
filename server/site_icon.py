"""The site's icon (browser tab, bookmarks, phone home screen). A PipsFarmer default ships with the site;
admins can upload their own logo in Admin → System."""
from __future__ import annotations

from .brand import THEME

import io
from pathlib import Path

from . import config

STATIC = Path(__file__).parent / "static" / "icons"
CUSTOM_DIR = config.ROOT / "data"
KINDS = {b"\x89PNG\r\n\x1a\n": ("png", "image/png"), b"\xff\xd8\xff": ("jpg", "image/jpeg"),
         b"\x00\x00\x01\x00": ("ico", "image/x-icon"), b"RIFF": ("webp", "image/webp")}
MAX_BYTES = 2_000_000


def sniff(data: bytes) -> tuple[str, str] | None:
    for magic, kind in KINDS.items():
        if data.startswith(magic) and (magic != b"RIFF" or data[8:12] == b"WEBP"):
            return kind
    return None


def custom() -> Path | None:
    for ext in ("png", "jpg", "ico", "webp"):
        p = CUSTOM_DIR / f"site-icon.{ext}"
        if p.exists():
            return p
    return None


def media_type(p: Path) -> str:
    return {"png": "image/png", "jpg": "image/jpeg", "ico": "image/x-icon", "webp": "image/webp"}[p.suffix[1:]]


_DEFAULT_V: str | None = None


def version() -> str:
    """Changes whenever the icon does (an upload, or a theme change rebuilding the default icons),
    so browsers and home-screen apps fetch the new one instead of keeping the old favicon."""
    global _DEFAULT_V
    p = custom()
    if p:
        return str(int(p.stat().st_mtime))
    if _DEFAULT_V is None:
        import hashlib
        h = hashlib.sha1()
        for name in ("favicon.ico", "icon.svg", "icon-192.png"):
            f = STATIC / name
            if f.exists():
                h.update(f.read_bytes())
        _DEFAULT_V = h.hexdigest()[:10]
    return _DEFAULT_V


def save(data: bytes) -> str | None:
    """Store an uploaded logo. Returns an error message, or None when saved."""
    if not data:
        return "Choose an image first."
    if len(data) > MAX_BYTES:
        return "That image is over 2 MB. Use a smaller one (512×512 is plenty)."
    kind = sniff(data)
    if not kind:
        return "Use a PNG, JPG, WEBP or ICO image."
    ext = kind[0]
    try:                                   # square it up and shrink it when Pillow is available
        from PIL import Image
        img = Image.open(io.BytesIO(data))
        img.load()
        if ext != "ico":
            img = img.convert("RGBA")
            side = max(img.size)
            canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
            canvas.paste(img, ((side - img.width) // 2, (side - img.height) // 2))
            canvas = canvas.resize((512, 512), Image.LANCZOS) if side > 512 else canvas
            buf = io.BytesIO()
            canvas.save(buf, "PNG")
            data, ext = buf.getvalue(), "png"
    except ImportError:
        pass
    except Exception:  # noqa: BLE001
        return "That image couldn't be read. Try saving it again as a PNG."
    clear()
    CUSTOM_DIR.mkdir(parents=True, exist_ok=True)
    (CUSTOM_DIR / f"site-icon.{ext}").write_bytes(data)
    return None


def clear() -> None:
    for ext in ("png", "jpg", "ico", "webp"):
        (CUSTOM_DIR / f"site-icon.{ext}").unlink(missing_ok=True)


def head_tags(admin: bool = False) -> str:
    v = version()
    tags = [f'<link rel="icon" href="/favicon.ico?v={v}" sizes="any">']
    if not custom():
        tags.append(f'<link rel="icon" type="image/svg+xml" href="/static/icons/icon.svg?v={v}">')
    tags += [f'<link rel="apple-touch-icon" href="/apple-touch-icon.png?v={v}">',
             f'<link rel="manifest" href="/{"admin" if admin else "site"}.webmanifest">',
             f'<meta name="theme-color" content="{THEME["bg"]}">']
    return "\n".join(tags)


def manifest(admin: bool = False) -> dict:
    """The web-app manifest: what Android and iOS use when the site is added to the home screen."""
    v = version()
    if custom():
        icons = [{"src": f"/apple-touch-icon.png?v={v}", "sizes": "512x512", "type": "image/png", "purpose": "any"}]
    else:
        icons = [{"src": f"/static/icons/icon-192.png?v={v}", "sizes": "192x192", "type": "image/png", "purpose": "any"},
                 {"src": f"/static/icons/icon-512.png?v={v}", "sizes": "512x512", "type": "image/png", "purpose": "any"},
                 {"src": f"/static/icons/icon-maskable-512.png?v={v}", "sizes": "512x512", "type": "image/png",
                  "purpose": "maskable"}]
    name = config.SITE_NAME + (" Admin" if admin else "")
    m = {"id": "/admin" if admin else "/", "name": name, "short_name": name,
         "description": "Gold signals copied to your MT5 account" if not admin else "PipsFarmer admin",
         "start_url": "/admin" if admin else "/dashboard", "scope": "/admin" if admin else "/",
         "display": "standalone", "display_override": ["standalone"], "background_color": THEME["bg"],
         "theme_color": THEME["bg"], "icons": icons, "categories": ["finance"]}
    if admin:
        m["shortcuts"] = [{"name": "Accounts", "url": "/admin/accounts"}, {"name": "Signals", "url": "/admin/signals"}]
    else:
        m["shortcuts"] = [{"name": "My accounts", "url": "/accounts"}, {"name": "Signals", "url": "/signals"}]
    return m
