"""Site icon: a default ships with the site; admins can upload their own logo."""
import io

from PIL import Image

from tests.test_web import admin_browser


def _png(w=800, h=400, colour=(255, 0, 0, 255)):
    buf = io.BytesIO()
    Image.new("RGBA", (w, h), colour).save(buf, "PNG")
    return buf.getvalue()


def test_default_icon_and_upload(browser):
    home = browser.get("/").text if hasattr(browser, "get") else browser.c.get("/").text
    assert 'href="/favicon.ico?v=' in home and "icon.svg" in home and "apple-touch-icon" in home
    c = browser.c if hasattr(browser, "c") else browser
    r = c.get("/favicon.ico")
    assert r.status_code == 200 and r.content[:4] == b"\x00\x00\x01\x00"
    assert c.get("/apple-touch-icon.png").content[:4] == b"\x89PNG"
    assert c.get("/site.webmanifest").json()["icons"]

    b = admin_browser(browser)
    page = b.get("/admin/system").text
    assert "Site icon" in page and "PipsFarmer icon (default)" in page
    csrf = page.split('name="csrf" value="')[1].split('"')[0]
    # not an image -> refused
    r = b.c.post("/admin/system/icon", data={"csrf": csrf}, files={"icon": ("x.svg", b"<svg onload=alert(1)>", "image/svg+xml")})
    assert "Use a PNG" in r.text
    r = b.c.post("/admin/system/icon", data={"csrf": csrf}, files={"icon": ("logo.png", _png(), "image/png")})
    assert r.status_code in (200, 303)
    icon = b.c.get("/favicon.ico")
    assert icon.headers["content-type"] == "image/png"
    img = Image.open(io.BytesIO(icon.content))
    assert img.size == (512, 512)                     # squared and shrunk
    home = b.c.get("/").text
    assert "icon.svg" not in home and "favicon.ico?v=d1" not in home
    b.c.post("/admin/system/icon", data={"csrf": csrf, "action": "default"})
    assert b.c.get("/favicon.ico").content[:4] == b"\x00\x00\x01\x00"
