"""First visit in a fresh / private browser: the page and the browser's icon requests go out together,
before the browser has a session cookie. An icon request must not replace the login page's session."""
from fastapi.testclient import TestClient

from server.db import Plan, SessionLocal, User
from server import security
from tests.conftest import csrf_of


def test_login_survives_parallel_icon_requests(app, browser):
    with SessionLocal() as s:
        if not s.query(User).filter_by(username="racer").first():
            s.add(User(username="racer", email="r@x.com", password_hash=security.hash_password("Racer-pass-123"),
                       plan_id=s.query(Plan).first().id))
            s.commit()
    page_client, icon_client = TestClient(app), TestClient(app)
    page = page_client.get("/login")
    token, cookie = csrf_of(page.text), page_client.cookies.get("pf_session")
    # the browser's other first-visit requests, sent without the cookie, at the same time
    for path in ("/apple-touch-icon-precomposed.png", "/favicon.ico", "/robots.txt", "/nope"):
        r = icon_client.get(path)
        if "pf_session" in r.cookies:      # whatever cookie these hand out would land in the same browser
            cookie = r.cookies["pf_session"]
    c = TestClient(app)
    c.cookies.set("pf_session", cookie)
    r = c.post("/login", data={"csrf": token, "username": "racer", "password": "Racer-pass-123"},
               follow_redirects=False)
    assert r.status_code in (302, 303) and r.headers["location"].endswith("/dashboard"), r.text[:300]


def test_expired_login_form_shows_the_form_again(app, browser):
    with SessionLocal() as s:
        if not s.query(User).filter_by(username="racer").first():
            s.add(User(username="racer", email="r@x.com", password_hash=security.hash_password("Racer-pass-123"),
                       plan_id=s.query(Plan).first().id))
            s.commit()
    c = TestClient(app)
    r = c.post("/login", data={"csrf": "old", "username": "racer", "password": "x"})
    assert r.status_code == 400 and 'name="password"' in r.text and "login cookie" in r.text
    # with a cookie but an old page: a fresh form, and the next try works
    c.get("/login")
    r = c.post("/login", data={"csrf": "old", "username": "racer", "password": "Racer-pass-123"})
    assert r.status_code == 400 and "expired" in r.text
    r = c.post("/login", data={"csrf": csrf_of(r.text), "username": "racer", "password": "Racer-pass-123"},
               follow_redirects=False)
    assert r.headers["location"].endswith("/dashboard")


def test_admin_and_customer_logins_share_a_browser(browser):
    from tests.test_web import admin_browser
    with SessionLocal() as s:
        if not s.query(User).filter_by(username="racer").first():
            s.add(User(username="racer", email="r@x.com", password_hash=security.hash_password("Racer-pass-123"),
                       plan_id=s.query(Plan).first().id))
            s.commit()
    b = admin_browser(browser)
    assert b.get("/admin").status_code == 200
    b.get("/login")
    r = b.post("/login", {"username": "racer", "password": "Racer-pass-123"})
    assert "/dashboard" in str(r.url) or r.status_code == 200
    assert b.get("/dashboard").status_code == 200
    page = b.get("/admin")
    assert page.status_code == 200 and "/admin/login" not in str(page.url)      # admin still logged in
