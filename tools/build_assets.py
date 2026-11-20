"""Build the public CSS/JS from server/assets_src into server/static, minified.

The readable sources stay in assets_src (never served); browsers only get the compact copies.
Run before packaging:  python tools/build_assets.py [path-to-esbuild]
(also rebuilds the logo/favicon/app icons from server/brand.py)
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC, OUT = ROOT / "server" / "assets_src", ROOT / "server" / "static"


def tiny_css(css: str) -> str:
    """Fallback when esbuild isn't around: drop comments and spare whitespace."""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    css = re.sub(r"\s+", " ", css)
    css = re.sub(r"\s*([{};,>])\s*", r"\1", css)
    css = re.sub(r":\s+", ":", css)
    return css.replace(";}", "}").strip()


def build(esbuild: str | None = None) -> None:
    esbuild = esbuild or shutil.which("esbuild")
    for name in ("app.js", "style.css"):
        src, out = SRC / name, OUT / name
        if esbuild:
            subprocess.run([esbuild, str(src), "--minify", "--legal-comments=none", "--target=es2018,safari13",
                            f"--outfile={out}", "--log-level=warning"], check=True)
        elif name.endswith(".css"):
            out.write_text(tiny_css(src.read_text(encoding="utf-8")), encoding="utf-8")
        else:
            out.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"{name}: {src.stat().st_size} -> {out.stat().st_size} bytes")


if __name__ == "__main__":
    build(sys.argv[1] if len(sys.argv) > 1 else None)
    # the favicon and app icons follow the theme colours in server/brand.py
    sys.path.insert(0, str(ROOT / "tools"))
    import build_icons
    build_icons.build()
