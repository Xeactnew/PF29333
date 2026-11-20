"""One place for the brand colours: the logo, favicon/app icons, manifest and offline pages all read from here.

Changing the theme? Edit THEME below *and* the :root block in assets_src/style.css, then run
`python tools/build_assets.py` (rebuilds the CSS/JS and the icon files from these colours).
"""
from __future__ import annotations

THEME = {
    "name": "Emerald and Champagne",
    "grad": ("#f0deb0", "#d9bf86", "#b5935a"),   # logo frame + brackets (light → deep champagne)
    "slash": "#efe9dc",                          # the / in </>
    "tile": "#0c1a14",                           # inside of the icon tile
    "bg": "#0c1a14",                             # page / app background (manifest + theme-color)
    "text": "#efe9dc",
    "dim": "#9fb3a8",
    "button_text": "#10201a",
}


def logo_svg(size: int | None = 26, gid: str = "pfg") -> str:
    a, b, c = THEME["grad"]
    dims = f' width="{size}" height="{size}"' if size else ""
    return (f'<svg xmlns="http://www.w3.org/2000/svg"{dims} viewBox="0 0 32 32" aria-hidden="true">'
            f'<defs><linearGradient id="{gid}" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="{a}"/>'
            f'<stop offset=".55" stop-color="{b}"/><stop offset="1" stop-color="{c}"/></linearGradient></defs>'
            f'<rect x="1.5" y="1.5" width="29" height="29" rx="8" fill="url(#{gid})"/>'
            f'<rect x="2.5" y="2.5" width="27" height="27" rx="7" fill="{THEME["tile"]}" opacity=".86"/>'
            f'<path d="M11.5 10.5 L6.5 16 L11.5 21.5" fill="none" stroke="url(#{gid})" stroke-width="2.4" '
            f'stroke-linecap="round" stroke-linejoin="round"/>'
            f'<path d="M20.5 10.5 L25.5 16 L20.5 21.5" fill="none" stroke="url(#{gid})" stroke-width="2.4" '
            f'stroke-linecap="round" stroke-linejoin="round"/>'
            f'<path d="M17.6 8.5 L14.4 23.5" stroke="{THEME["slash"]}" stroke-width="2.2" stroke-linecap="round"/></svg>')
