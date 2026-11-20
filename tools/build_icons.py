"""Rebuild the logo, favicon and app icons from server/brand.py (run whenever the theme colours change).

Writes server/static/icons/icon.svg always; the PNG/ICO sizes need a Chrome + playwright to render
(used on the build machine; on a VPS without them the existing PNGs are kept).
"""
from __future__ import annotations

import asyncio
import io
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from server.brand import THEME, logo_svg  # noqa: E402

ICONS = ROOT / "server" / "static" / "icons"


async def _render(svg: str, size: int, pad: float = 0.0, bg: str | None = None):
    from PIL import Image
    from playwright.async_api import async_playwright
    chrome = shutil.which("google-chrome") or shutil.which("chromium") or "/usr/bin/google-chrome"
    async with async_playwright() as p:
        b = await p.chromium.launch(executable_path=chrome, args=["--no-sandbox"])
        pg = await b.new_page(viewport={"width": size, "height": size})
        inner = int(size * (1 - 2 * pad))
        body = svg.replace("<svg ", f'<svg width="{inner}" height="{inner}" ', 1)
        await pg.set_content(f'<html><body style="margin:0;background:{bg or "transparent"};display:grid;'
                             f'place-items:center;width:{size}px;height:{size}px">{body}</body></html>')
        png = await pg.screenshot(omit_background=bg is None)
        await b.close()
    return Image.open(io.BytesIO(png)).convert("RGBA")


async def _pngs(svg: str) -> None:
    bg = THEME["bg"]
    for name, size, fill in (("favicon-32.png", 32, None), ("apple-touch-icon.png", 180, bg),
                             ("icon-192.png", 192, None), ("icon-512.png", 512, None)):
        (await _render(svg, size, bg=fill)).save(ICONS / name)
    (await _render(svg, 512, pad=0.12, bg=bg)).save(ICONS / "icon-maskable-512.png")
    (await _render(svg, 256)).save(ICONS / "favicon.ico", sizes=[(16, 16), (32, 32), (48, 48), (64, 64)])


def build() -> None:
    svg = logo_svg(None, "g")
    (ICONS / "icon.svg").write_text(svg + "\n", encoding="utf-8")
    try:
        asyncio.run(_pngs(svg))
        print(f"icons rebuilt in {THEME['name']} colours")
    except Exception as e:  # noqa: BLE001
        print(f"icon.svg updated; PNG/ICO not rendered here ({e.__class__.__name__}): run this on the build machine")


if __name__ == "__main__":
    build()
