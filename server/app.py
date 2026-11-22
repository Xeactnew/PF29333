"""PipsFarmer web server. Run with:  python run_server.py"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.gzip import GZipMiddleware
from starlette.middleware.sessions import SessionMiddleware

from . import admin, api, config, customer, scheduler, site_icon, system, telegram_service
from .db import init_engine
from .web import ASSET_V, Redirect, redirect, render

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telethon").setLevel(logging.WARNING)
log = logging.getLogger("pf.app")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_engine()
    if not config.DISABLE_BACKGROUND:
        system.setup_file_logging()
        logging.getLogger("pf").info("PipsFarmer %s starting", system.VERSION)
    tasks = []
    if not config.DISABLE_BACKGROUND:
        from . import research
        tasks = [asyncio.create_task(scheduler.run_all()), asyncio.create_task(telegram_service.run_all()),
                 asyncio.create_task(research.run_once_after_install())]
    yield
    for t in tasks:
        t.cancel()


ASSET_PATHS = ("/static/", "/favicon.ico", "/apple-touch-icon", "/site.webmanifest", "/admin.webmanifest",
               "/sw.js", "/offline", "/health")

SERVICE_WORKER = (
    'const C="pf-__VERSION__",O="/offline";'
    'self.addEventListener("install",e=>{e.waitUntil(caches.open(C).then(c=>c.addAll([O,"/static/icons/icon-192.png"])));self.skipWaiting()});'
    'self.addEventListener("activate",e=>{e.waitUntil(caches.keys().then(k=>Promise.all(k.filter(x=>x!==C).map(x=>caches.delete(x)))));self.clients.claim()});'
    'self.addEventListener("fetch",e=>{const r=e.request;if(r.method!=="GET"||r.mode!=="navigate")return;const u=new URL(r.url);'
    'if(u.origin!==location.origin||u.pathname.startsWith("/admin")||u.pathname.startsWith("/api/"))return;'
    'e.respondWith(fetch(r).catch(()=>caches.match(O)))});'
)

_ERROR_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Something went wrong</title>
<style>html,body{margin:0;height:100%;background:__BG__;color:__TEXT__;font:16px/1.5 system-ui,sans-serif}main{min-height:100%;display:grid;place-items:center;text-align:center;padding:24px}p{color:__DIM__}a{color:__C2__}</style></head>
<body><main><div><h1>Something went wrong</h1><p>Please try again in a moment.</p><a href="/">Back to the start</a></div></main></body></html>"""

_OFFLINE_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="__BG__"><title>Offline</title>
<style>html,body{margin:0;height:100%;background:__BG__;color:__TEXT__;font:16px/1.5 system-ui,-apple-system,sans-serif}
main{min-height:100%;display:grid;place-items:center;text-align:center;padding:24px}
img{width:72px;height:72px;border-radius:18px}h1{font-size:22px;margin:18px 0 6px}p{color:__DIM__;margin:0 0 22px}
button{font:600 16px system-ui;color:__BTN__;border:0;border-radius:999px;padding:13px 26px;
background:linear-gradient(118deg,__C1__,__C2__ 52%,__C3__)}</style></head>
<body><main><div><img src="/static/icons/icon-192.png" alt=""><h1>No connection</h1>
<p>Your trades keep running on our servers.<br>Reconnect to see them.</p>
<button onclick="location.reload()">Try again</button></div></main></body></html>"""

def _themed(html: str) -> str:
    from .brand import THEME
    c1, c2, c3 = THEME["grad"]
    for k, v in {"__BG__": THEME["bg"], "__TEXT__": THEME["text"], "__DIM__": THEME["dim"], "__BTN__": THEME["button_text"],
                 "__C1__": c1, "__C2__": c2, "__C3__": c3}.items():
        html = html.replace(k, v)
    return html


ERROR_PAGE = _themed(_ERROR_PAGE)
OFFLINE_PAGE = _themed(_OFFLINE_PAGE)


def create_app() -> FastAPI:
    problems = config.check()
    if problems:
        raise SystemExit("Fix your .env first:\n - " + "\n - ".join(problems))
    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(SessionMiddleware, secret_key=config.SECRET_KEY, session_cookie="pf_session",
                       max_age=60 * 60 * 24 * 14, same_site="lax", https_only=config.HTTPS_ONLY_COOKIES)
    app.add_middleware(GZipMiddleware, minimum_size=700)   # compress HTML/CSS/JS over the wire
    app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")
    app.include_router(customer.router)
    app.include_router(admin.router)
    app.include_router(api.router)

    @app.exception_handler(Redirect)
    async def _redirect(request: Request, exc: Redirect):
        return redirect(exc.url)

    @app.exception_handler(HTTPException)
    async def _http(request: Request, exc: HTTPException):
        if request.url.path.startswith("/api/"):
            # plain answers: nothing that says what the site is built with
            return JSONResponse({"error": exc.detail if exc.status_code < 500 else "error"}, status_code=exc.status_code)
        titles = {404: "Not found", 400: "Something's off with that request", 401: "Log in first"}
        return render(request, "message.html", {"title": titles.get(exc.status_code, "Error"),
                                                "text": str(exc.detail or "")}, exc.status_code)

    @app.exception_handler(StarletteHTTPException)
    async def _starlette_http(request: Request, exc: StarletteHTTPException):
        # unknown pages and wrong methods: our own plain page (no session, so it can't upset a login),
        # not the framework's default reply
        if request.url.path.startswith("/api/"):
            return JSONResponse({"error": "not found" if exc.status_code == 404 else "error"}, status_code=exc.status_code)
        title = "Page not found" if exc.status_code == 404 else "That didn't work"
        return HTMLResponse(ERROR_PAGE.replace("Something went wrong", title, 2), status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def _invalid(request: Request, exc: RequestValidationError):
        return await _http(request, HTTPException(400, "That request wasn't valid."))

    @app.exception_handler(Exception)
    async def _crash(request: Request, exc: Exception):
        log.exception("unhandled error on %s", request.url.path)
        if request.url.path.startswith("/api/"):
            return JSONResponse({"error": "error"}, status_code=500)
        return HTMLResponse(ERROR_PAGE, status_code=500)

    @app.get("/robots.txt", include_in_schema=False)
    def robots():
        from fastapi.responses import PlainTextResponse
        return PlainTextResponse("User-agent: *\nDisallow: /admin\nDisallow: /api/\nDisallow: /accounts\n"
                                 "Disallow: /dashboard\n")

    @app.middleware("http")
    async def _headers(request: Request, call_next):
        resp = await call_next(request)
        if request.url.path.startswith(ASSET_PATHS):
            resp.raw_headers[:] = [(k, v) for k, v in resp.raw_headers if k != b"set-cookie"]
            if request.url.path.startswith("/static/"):
                # links carry ?v=<fingerprint>, so a copy can be kept a year; without it, always check
                resp.headers["Cache-Control"] = ("public, max-age=31536000, immutable" if request.query_params.get("v")
                                                 else "no-cache")
        # behind the Cloudflare tunnel the site is https: mark the login cookie Secure automatically
        if request.headers.get("x-forwarded-proto") == "https" or request.url.scheme == "https":
            raw = [(k, v + b"; secure" if k == b"set-cookie" and b"pf_session=" in v and b"secure" not in v.lower() else v)
                   for k, v in resp.raw_headers]
            resp.raw_headers[:] = raw
            resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        resp.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()")
        resp.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        if request.url.path.startswith(("/admin", "/api/", "/accounts", "/dashboard")):
            resp.headers.setdefault("X-Robots-Tag", "noindex, nofollow")
        for k in ("server", "x-powered-by"):
            if k in resp.headers:
                del resp.headers[k]
        return resp

    def _icon(default: str):
        p = site_icon.custom()
        if p:
            return FileResponse(p, media_type=site_icon.media_type(p), headers={"Cache-Control": "public, max-age=86400"})
        return FileResponse(site_icon.STATIC / default, headers={"Cache-Control": "public, max-age=86400"})

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon():
        return _icon("favicon.ico")

    @app.get("/apple-touch-icon.png", include_in_schema=False)
    def apple_icon():
        return _icon("apple-touch-icon.png")

    @app.get("/site.webmanifest", include_in_schema=False)
    def manifest():
        return JSONResponse(site_icon.manifest(), media_type="application/manifest+json")

    @app.get("/admin.webmanifest", include_in_schema=False)
    def admin_manifest():
        return JSONResponse(site_icon.manifest(admin=True), media_type="application/manifest+json")

    @app.get("/sw.js", include_in_schema=False)
    def service_worker():
        from fastapi.responses import Response
        js = SERVICE_WORKER.replace("__VERSION__", ASSET_V)
        return Response(js, media_type="application/javascript", headers={"Cache-Control": "no-cache"})

    @app.get("/offline", response_class=HTMLResponse, include_in_schema=False)
    def offline():
        return OFFLINE_PAGE

    @app.get("/health", response_class=HTMLResponse)
    def health():
        return "ok"

    return app
