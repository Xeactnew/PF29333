"""Home-screen app: manifests, service worker, offline page, phone tab bars."""
from tests.test_web import admin_browser


def test_app_files(browser):
    c = browser.c
    m = c.get("/site.webmanifest").json()
    assert m["display"] == "standalone" and m["start_url"] == "/dashboard" and m["background_color"] == "#0c1a14"
    assert any(i.get("purpose") == "maskable" for i in m["icons"])
    a = c.get("/admin.webmanifest").json()
    assert a["start_url"] == "/admin" and a["scope"] == "/admin" and a["name"].endswith("Admin")
    sw = c.get("/sw.js")
    assert sw.headers["content-type"].startswith("application/javascript") and "caches.match" in sw.text
    assert "/admin" in sw.text                     # admin pages never go through the offline cache
    off = c.get("/offline")
    assert off.status_code == 200 and "No connection" in off.text
    home = c.get("/").text
    assert "viewport-fit=cover" in home and "apple-mobile-web-app-capable" in home and "black-translucent" in home
    assert 'href="/site.webmanifest"' in home


def test_assets_never_reset_the_login_cookie(browser):
    c = browser.c
    c.get("/login")
    assert "pf_session" in c.cookies
    for path in ("/static/style.css", "/favicon.ico", "/site.webmanifest", "/sw.js", "/offline"):
        assert "set-cookie" not in c.get(path).headers, path


def test_phone_bars(browser):
    b = admin_browser(browser)
    page = b.get("/admin/accounts").text
    assert 'class="tabbar"' in page and 'data-sheet="more-sheet"' in page and "/admin/system" in page
    assert 'href="/admin.webmanifest"' in page and 'class="appbar"' in page
    # "More" lights up on a page that lives in the sheet
    sys_page = b.get("/admin/system").text
    assert 'class="tab on" type="button" data-sheet' in sys_page


def test_css_and_js_links_are_versioned(browser):
    c = browser.c
    page = c.get("/login").text
    import re
    css = re.search(r'href="(/static/style\.css\?v=[0-9a-f]+)"', page).group(1)
    js = re.search(r'src="(/static/app\.js\?v=[0-9a-f]+)"', page).group(1)
    assert "immutable" in c.get(css).headers["cache-control"] and "immutable" in c.get(js).headers["cache-control"]
    assert c.get("/static/style.css").headers["cache-control"] == "no-cache"
    assert ".tabbar,.sheet{display:none}" in page.replace(".appbar,", "")  # hidden even with an old stylesheet
