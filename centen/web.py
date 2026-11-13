"""centen as a server (a NAS, a home server, a Raspberry Pi): one process that

- serves the dashboard (`/`), the update app (`/bijwerken`) and the weekly digest (`/digest`) behind a login,
- offers the MCP tools over HTTP at `/mcp` (Bearer key), for Claude Desktop via `centen remote`,
- runs the schedule itself: bank sync twice a day, prices every hour, price history daily, crypto (Bitvavo API,
  hardware wallets) daily, the digest weekly.

Start with `centen web` (Docker: the image's default command). Access key: env CENTEN_KEY, else generated once and
kept in the data folder (`web_key`); `centen web --show-key` prints it. Meant for your home network or a VPN
(Tailscale, WireGuard): don't expose it to the internet without a reverse proxy with HTTPS in front.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import time
from contextlib import closing
from datetime import date, datetime, timedelta
from pathlib import Path

from . import config, db

log = logging.getLogger("centen.web")
COOKIE = "centen_session"


# --- key and sessions -----------------------------------------------------------------------------

def access_key() -> str:
    k = os.environ.get("CENTEN_KEY")
    if k:
        return k.strip()
    p = config.home() / "web_key"
    if p.exists():
        return p.read_text(encoding="utf-8").strip()
    k = secrets.token_urlsafe(24)
    p.write_text(k, encoding="utf-8")
    try:
        p.chmod(0o600)
    except OSError:
        pass
    return k


def _session_value(key: str) -> str:
    return hmac.new(key.encode(), b"centen-session-v1", hashlib.sha256).hexdigest()


def _csrf(key: str) -> str:
    return hmac.new(key.encode(), b"centen-csrf-v1", hashlib.sha256).hexdigest()[:32]


# --- schedule -------------------------------------------------------------------------------------

DEFAULT_SCHEDULE = {"sync": ["07:40", "18:40"], "prices_from": 8, "prices_to": 22, "digest": "mon 08:20",
                    "alerts": "08:30", "crypto": "07:55"}


def _schedule_cfg() -> dict:
    try:
        return {**DEFAULT_SCHEDULE, **(config.load().get("schedule") or {})}
    except Exception:  # noqa: BLE001
        return dict(DEFAULT_SCHEDULE)


def _job(name, fn):
    try:
        with closing(db.connect()) as c:
            res = fn(c)
        log.info("%s: %s", name, json.dumps(res, default=str)[:300] if res is not None else "ok")
    except Exception as e:  # noqa: BLE001 - a failing job never stops the server
        log.warning("%s failed: %s", name, e)


def _sync(c):
    from . import dashboard, enablebanking, history, prices
    cfg = config.load()
    if not cfg["enablebanking"].get("app_id"):
        return {"skipped": "no bank connection configured"}
    res = enablebanking.sync(c, cfg)
    with contextlib.suppress(Exception):
        prices.refresh(c, max_age_minutes=15)
    with contextlib.suppress(Exception):
        history.ensure_history(c)
    dashboard.refresh(c)
    return {k: v for k, v in (res or {}).items() if k != "accounts"} if isinstance(res, dict) else res


def _prices(c):
    from . import dashboard, history, prices
    res = prices.refresh(c, max_age_minutes=0)
    with contextlib.suppress(Exception):
        history.ensure_history(c)
    dashboard.refresh(c)
    return {"updated": res.get("updated"), "missing": len(res.get("missing") or [])}


def _crypto(c):
    from . import crypto_sync, dashboard
    st = crypto_sync.status(c)
    if not st["bitvavo"]["configured"] and not st["wallets"]:
        return {"skipped": "no Bitvavo key or wallets"}
    res = crypto_sync.sync(c)
    dashboard.refresh(c)
    return res


def _digest(c):
    from . import digest, notify
    d, p = digest.write(c)
    if notify.configured():
        notify.send_digest(c, d["headline"])
    return d["headline"]


def _alerts(c):
    from . import notify
    if not notify.configured():
        return {"skipped": "notifications not configured"}
    return notify.run_alerts(c)


def scheduler(stop: threading.Event):
    done: set[str] = set()
    while not stop.wait(30):
        now = datetime.now()
        hm, today = now.strftime("%H:%M"), now.date().isoformat()
        s = _schedule_cfg()
        for t in s["sync"]:
            if hm == t and f"sync {today} {t}" not in done:
                done.add(f"sync {today} {t}")
                _job("sync", _sync)
        if s["prices_from"] <= now.hour <= s["prices_to"] and now.minute == 5 and f"prices {today} {now.hour}" not in done:
            done.add(f"prices {today} {now.hour}")
            if now.weekday() < 5 or now.hour in (10, 18):  # weekends: crypto twice a day is enough
                _job("prices", _prices)
        dday, dtime = (s["digest"].split() + ["08:20"])[:2]
        if now.strftime("%a").lower().startswith(dday[:3].lower()) and hm == dtime and f"digest {today}" not in done:
            done.add(f"digest {today}")
            _job("digest", _digest)
        if hm == s.get("crypto") and f"crypto {today}" not in done:
            done.add(f"crypto {today}")
            _job("crypto", _crypto)
        if hm == s.get("alerts") and f"alerts {today}" not in done:
            done.add(f"alerts {today}")
            _job("alerts", _alerts)
        if len(done) > 500:
            done = {x for x in done if today in x}


# --- web app --------------------------------------------------------------------------------------

LOGIN = """<!doctype html><html lang=nl><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>centen</title><style>body{font:15px system-ui,Segoe UI,sans-serif;background:#f7f6f2;color:#0b0b0b;display:grid;
place-items:center;height:100vh;margin:0}@media(prefers-color-scheme:dark){body{background:#141413;color:#f0efec}}
form{background:rgba(127,127,127,.08);padding:28px;border-radius:14px;min-width:300px}h1{font-size:20px;margin:0 0 14px}
input{font:inherit;width:100%;padding:8px 10px;border-radius:8px;border:1px solid #c3c2b7;margin:6px 0 12px;box-sizing:border-box}
button{font:inherit;padding:8px 16px;border-radius:8px;border:0;background:#2a78d6;color:#fff;cursor:pointer}
p{color:#898781;font-size:13px}.e{color:#c62f2e}</style>
<form method=post action=/login><h1>centen</h1><label for=k>Toegangssleutel</label>
<input id=k name=key type=password autocomplete=current-password autofocus>__ERR__<button>Aanmelden</button>
<p>De sleutel staat in de map met je gegevens (web_key) of in de omgevingsvariabele CENTEN_KEY.</p></form></html>"""

NAV = """<style>
.centen-nav{display:flex;gap:4px;align-items:center;flex-wrap:wrap;padding:8px 16px;font:500 13.5px system-ui,-apple-system,
"Segoe UI",sans-serif;background:#ffffff;border-bottom:1px solid #e1e0d9}
.centen-nav b{margin-right:10px;font-weight:650;color:#0b0b0b}
.centen-nav a{color:#2c2c2a;text-decoration:none;padding:5px 11px;border-radius:7px}
.centen-nav a:hover,.centen-nav a:focus-visible{background:#efeee9;color:#0b0b0b;outline:none}
.centen-nav a[aria-current]{background:#0b0b0b;color:#ffffff}
.centen-nav .sp{flex:1}
@media (max-width:600px){.centen-nav{flex-wrap:nowrap;overflow-x:auto;padding:6px 10px;gap:2px}
.centen-nav b{margin-right:4px}.centen-nav a{padding:6px 9px;white-space:nowrap}}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]) .centen-nav{background:#1d1d1b;border-color:#383835}
:root:not([data-theme="light"]) .centen-nav b{color:#f0efec}
:root:not([data-theme="light"]) .centen-nav a{color:#e6e5df}
:root:not([data-theme="light"]) .centen-nav a:hover{background:#2c2c2a;color:#ffffff}
:root:not([data-theme="light"]) .centen-nav a[aria-current]{background:#f0efec;color:#141413}}
:root[data-theme="dark"] .centen-nav{background:#1d1d1b;border-color:#383835}
:root[data-theme="dark"] .centen-nav b{color:#f0efec}
:root[data-theme="dark"] .centen-nav a{color:#e6e5df}
:root[data-theme="dark"] .centen-nav a:hover{background:#2c2c2a;color:#ffffff}
:root[data-theme="dark"] .centen-nav a[aria-current]{background:#f0efec;color:#141413}
</style><nav class="centen-nav" aria-label="centen"><b>centen</b><a href="/" __A_/__>Dashboard</a>
<a href="/bijwerken" __A_/bijwerken__>Bijwerken</a><a href="/digest" __A_/digest__>Weekoverzicht</a>
<span class="sp"></span><a href="/logout">Afmelden</a></nav>"""


def nav(current: str) -> str:
    out = NAV
    for p in ("/", "/bijwerken", "/digest"):
        out = out.replace(f"__A_{p}__", 'aria-current="page"' if p == current else "")
    return out


def build_app(key: str | None = None, run_scheduler: bool = True):
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
    from starlette.routing import Mount, Route

    from . import app as upd
    from . import dashboard, pwa
    from .server import server as mcp

    key = key or access_key()
    sess, csrf = _session_value(key), _csrf(key)
    mcp_app = mcp.streamable_http_app(streamable_http_path="/mcp", json_response=True, stateless_http=True,
                                      host="0.0.0.0")

    def logged_in(req: Request) -> bool:
        return hmac.compare_digest(req.cookies.get(COOKIE, ""), sess)

    def guard(req: Request, api: bool = False):
        if not logged_in(req):
            return JSONResponse({"error": "niet aangemeld"}, 401) if api else RedirectResponse("/login", 303)
        if api and req.method == "POST" and not hmac.compare_digest(req.headers.get("x-centen-key", ""), csrf):
            return JSONResponse({"error": "forbidden"}, 403)
        if api and req.method == "GET" and not hmac.compare_digest(req.headers.get("x-centen-key", ""), csrf):
            return JSONResponse({"error": "forbidden"}, 403)
        return None

    def secure_headers(r: Response) -> Response:
        r.headers["X-Frame-Options"] = "DENY"
        r.headers["Cache-Control"] = "no-store"
        r.headers["Referrer-Policy"] = "no-referrer"
        return r

    async def login(req: Request):
        if req.method == "GET":
            return secure_headers(HTMLResponse(LOGIN.replace("__ERR__", "")))
        form = await req.form()
        if hmac.compare_digest(str(form.get("key", "")).strip(), key):
            r = RedirectResponse("/", 303)
            r.set_cookie(COOKIE, sess, httponly=True, samesite="strict", max_age=60 * 60 * 24 * 30,
                         secure=req.url.scheme == "https")
            return r
        await asyncio.sleep(1)
        return secure_headers(HTMLResponse(LOGIN.replace("__ERR__", "<p class=e>Verkeerde sleutel.</p>"), 401))

    async def logout(req: Request):
        r = RedirectResponse("/login", 303)
        r.delete_cookie(COOKIE)
        r.headers["Clear-Site-Data"] = '"cache"'  # the phone's offline copy of the dashboard goes too
        return r

    def _dash_file() -> Path:
        p = config.home() / "dashboard.html"
        if not p.exists() or time.time() - p.stat().st_mtime > 900:
            with closing(db.connect()) as c:
                dashboard.build(c, p)
        return p

    async def index(req: Request):
        if (g := guard(req)):
            return g
        html = _dash_file().read_text(encoding="utf-8").replace("<body>", "<body>" + nav("/"), 1)
        return secure_headers(HTMLResponse(pwa.inject(html)))

    async def update_page(req: Request):
        if (g := guard(req)):
            return g
        html = upd.TEMPLATE.read_text(encoding="utf-8").replace("__TOKEN__", csrf).replace("<body>", "<body>" + nav("/bijwerken"), 1)
        return secure_headers(HTMLResponse(pwa.inject(html)))

    async def digest_page(req: Request):
        if (g := guard(req)):
            return g
        p = config.home() / "digest.html"
        if not p.exists():
            with closing(db.connect()) as c:
                _digest(c)
        return secure_headers(HTMLResponse(pwa.inject(p.read_text(encoding="utf-8").replace("<body>", "<body>" + nav("/digest"), 1))))

    async def api_state(req: Request):
        if (g := guard(req, api=True)):
            return g
        with closing(db.connect()) as c:
            return JSONResponse(json.loads(json.dumps(upd.state(c), default=str)))

    async def api_act(req: Request):
        if (g := guard(req, api=True)):
            return g
        try:
            body = await req.json()
            with closing(db.connect()) as c:
                res = upd.act(c, req.path_params["action"], body)
                dashboard.refresh(c)
            return JSONResponse({"ok": True, "result": json.loads(json.dumps(res, default=str))})
        except (ValueError, KeyError) as e:
            return JSONResponse({"error": str(e)}, 400)

    async def api_upload(req: Request):
        if (g := guard(req, api=True)):
            return g
        data = await req.body()
        if len(data) > 30 * 1024 * 1024:
            return JSONResponse({"error": "Bestand te groot (max 30 MB)."}, 413)
        q = req.query_params
        try:
            with closing(db.connect()) as c:
                res = upd.upload(c, q.get("kind", "bank"), q.get("account"), q.get("name", "export.csv"), data)
                dashboard.refresh(c)
            return JSONResponse({"ok": True, "result": json.loads(json.dumps(res, default=str))})
        except (ValueError, KeyError) as e:
            return JSONResponse({"error": str(e)}, 400)

    async def health(req: Request):
        return PlainTextResponse("ok")

    async def manifest(req: Request):
        return JSONResponse(pwa.MANIFEST, media_type="application/manifest+json")

    async def service_worker(req: Request):
        return Response(pwa.SERVICE_WORKER, media_type="text/javascript",
                        headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})

    async def icon(req: Request):
        b = pwa.icon(req.url.path.lstrip("/"))
        if b is None:
            return PlainTextResponse("not found", 404)
        return Response(b, media_type="image/png", headers={"Cache-Control": "public, max-age=604800"})

    class BearerMCP:
        """/mcp: Bearer <key> required."""

        def __init__(self, inner):
            self.inner = inner

        async def __call__(self, scope, receive, send):
            if scope["type"] == "http":
                auth = dict(scope.get("headers") or []).get(b"authorization", b"").decode()
                if not hmac.compare_digest(auth, f"Bearer {key}"):
                    await JSONResponse({"error": "unauthorized"}, 401)(scope, receive, send)
                    return
            await self.inner(scope, receive, send)

    stop = threading.Event()

    @contextlib.asynccontextmanager
    async def lifespan(app):
        if run_scheduler:
            threading.Thread(target=scheduler, args=(stop,), daemon=True, name="centen-scheduler").start()
        async with mcp.session_manager.run():
            yield
        stop.set()

    routes = [Route("/", index), Route("/login", login, methods=["GET", "POST"]), Route("/logout", logout),
              Route("/bijwerken", update_page), Route("/digest", digest_page), Route("/healthz", health),
              Route("/api/state", api_state), Route("/api/act/{action}", api_act, methods=["POST"]),
              Route("/api/upload", api_upload, methods=["POST"]),
              Route("/manifest.webmanifest", manifest), Route("/sw.js", service_worker),
              *[Route(f"/{n}", icon) for n in pwa.ICONS],
              Mount("/", app=BearerMCP(mcp_app))]
    return Starlette(routes=routes, lifespan=lifespan)


def serve(host: str = "0.0.0.0", port: int = 8770, run_scheduler: bool = True):
    import uvicorn
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    with closing(db.connect()):
        pass  # create / migrate the database before the first request
    key = access_key()
    log.info("centen web on http://%s:%s  (dashboard /, update /bijwerken, MCP /mcp)", host, port)
    if not os.environ.get("CENTEN_KEY"):
        log.info("access key in %s", config.home() / "web_key")
    uvicorn.run(build_app(key, run_scheduler), host=host, port=port, log_level="warning", proxy_headers=True)
