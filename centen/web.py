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


MIN_KEY = 20
SESSION_DAYS = 30


def key_fingerprint(key: str) -> str:
    return hashlib.sha256(b"centen-key-v2|" + key.encode()).hexdigest()[:16]


def _csrf(key: str, session: str = "") -> str:
    """Per session: a token leaked from one browser is useless in another."""
    return hmac.new(key.encode(), b"centen-csrf-v2|" + session.encode(), hashlib.sha256).hexdigest()[:32]


# the jobs moved to jobs.py in r22; these names stay importable from here
from .jobs import DEFAULT_SCHEDULE, _alerts, _crypto, _digest, _prices, _sync  # noqa: E402,F401
from .jobs import schedule_cfg as _schedule_cfg  # noqa: E402,F401


# --- sessions (r22): a random id per browser, only its hash stored; logout and "log out all" really end it ----------

def _hash(sid: str) -> str:
    return hashlib.sha256(sid.encode()).hexdigest()


def session_new(conn, key: str, agent: str | None) -> str:
    sid = secrets.token_urlsafe(32)
    now = datetime.now().isoformat(timespec="seconds")
    conn.execute("INSERT INTO web_sessions(id_hash, key_fp, created, last_seen, agent) VALUES (?,?,?,?,?)",
                 (_hash(sid), key_fingerprint(key), now, now, _agent(agent)))
    conn.execute("DELETE FROM web_sessions WHERE last_seen < ? OR key_fp <> ?",
                 ((datetime.now() - timedelta(days=SESSION_DAYS)).isoformat(), key_fingerprint(key)))
    conn.commit()
    return sid


def session_check(conn, key: str, sid: str | None) -> bool:
    if not sid:
        return False
    r = conn.execute("SELECT key_fp, last_seen FROM web_sessions WHERE id_hash=?", (_hash(sid),)).fetchone()
    if not r or not hmac.compare_digest(r["key_fp"], key_fingerprint(key)):
        return False
    seen = datetime.fromisoformat(r["last_seen"])
    if datetime.now() - seen > timedelta(days=SESSION_DAYS):
        return False
    if datetime.now() - seen > timedelta(hours=1):  # sliding: 30 days after the last visit
        conn.execute("UPDATE web_sessions SET last_seen=? WHERE id_hash=?",
                     (datetime.now().isoformat(timespec="seconds"), _hash(sid)))
        conn.commit()
    return True


def session_end(conn, sid: str | None, others: bool = False) -> int:
    if others:
        n = conn.execute("DELETE FROM web_sessions WHERE id_hash <> ?", (_hash(sid or ""),)).rowcount
    else:
        n = conn.execute("DELETE FROM web_sessions WHERE id_hash=?", (_hash(sid or ""),)).rowcount
    conn.commit()
    return n


def sessions(conn, sid: str | None) -> list[dict]:
    me = _hash(sid or "")
    return [{"agent": r["agent"], "created": r["created"], "last_seen": r["last_seen"], "current": r["id_hash"] == me}
            for r in conn.execute("SELECT * FROM web_sessions ORDER BY last_seen DESC")]


def _agent(ua: str | None) -> str:
    """A short, readable device name from the User-Agent ("iPhone · Safari"), nothing more is kept."""
    ua = ua or ""
    dev = next((n for k, n in (("iPhone", "iPhone"), ("iPad", "iPad"), ("Android", "Android"), ("Windows", "Windows"),
                               ("Macintosh", "Mac"), ("Linux", "Linux")) if k in ua), "")
    br = next((n for k, n in (("Edg/", "Edge"), ("Firefox/", "Firefox"), ("Chrome/", "Chrome"), ("Safari/", "Safari"))
               if k in ua), "")
    return " · ".join(x for x in (dev, br) if x) or "onbekend"


class LoginLimiter:
    """At most `limit` wrong keys per address per `window` seconds; then 429 until the window has passed."""

    def __init__(self, limit: int = 5, window: int = 600):
        self.limit, self.window, self.fails = limit, window, {}

    def blocked(self, ip: str) -> bool:
        now = time.monotonic()
        self.fails[ip] = [t for t in self.fails.get(ip, []) if now - t < self.window]
        return len(self.fails[ip]) >= self.limit

    def fail(self, ip: str) -> None:
        self.fails.setdefault(ip, []).append(time.monotonic())
        if len(self.fails) > 1000:
            self.fails = {k: v for k, v in self.fails.items() if v}

    def ok(self, ip: str) -> None:
        self.fails.pop(ip, None)


# --- dashboard: rebuilt in the background, at most one build at a time ----------------------------------------------

class DashboardBuilder:
    """Changes mark the dashboard dirty; a background thread rebuilds it a moment later, so several saves in a row give
    one build and no request ever waits for one, except the very first page view when there is no dashboard yet."""

    def __init__(self, delay: float = 1.5, max_age: int = 900):
        self.delay, self.max_age = delay, max_age
        self.lock = threading.Lock()
        self.dirty = threading.Event()
        self._timer: threading.Timer | None = None

    def path(self) -> Path:
        return config.home() / "dashboard.html"

    def build_now(self) -> Path:
        from . import dashboard
        with self.lock:
            self.dirty.clear()
            with closing(db.connect()) as c:
                return dashboard.build(c, self.path())

    def mark_dirty(self) -> None:
        self.dirty.set()
        if self._timer:
            self._timer.cancel()
        self._timer = threading.Timer(self.delay, self._background)
        self._timer.daemon = True
        self._timer.start()

    def _background(self) -> None:
        try:
            self.build_now()
        except Exception as e:  # noqa: BLE001
            log.warning("dashboard build failed: %s", e)

    def current(self) -> Path:
        """The dashboard file to serve (call from a worker thread). Builds only when missing; stale or dirty ones are
        served as they are while a new one is being made."""
        p = self.path()
        if not p.exists():
            return self.build_now()
        if self.dirty.is_set() or time.time() - p.stat().st_mtime > self.max_age:
            if not self.lock.locked():
                threading.Thread(target=self._background, daemon=True).start()
        return p


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
.centen-nav{display:flex;gap:4px;align-items:center;padding:8px 16px;font:500 13.5px system-ui,-apple-system,
"Segoe UI",sans-serif;background:#ffffff;border-bottom:1px solid #e1e0d9;position:relative}
.centen-nav b{margin-right:10px;font-weight:650;color:#0b0b0b}
.centen-nav a{color:#2c2c2a;text-decoration:none;padding:5px 11px;border-radius:7px;white-space:nowrap}
.centen-nav a:hover,.centen-nav a:focus-visible{background:#efeee9;color:#0b0b0b;outline:none}
.centen-nav a[aria-current]{background:#0b0b0b;color:#ffffff}
.centen-nav .sp{flex:1}
.centen-nav .st{display:flex;gap:10px;font-size:12px;color:#6b6a65;padding:4px 8px;border-radius:7px}
.centen-nav .st span{display:inline-flex;align-items:center;gap:5px;white-space:nowrap}
.centen-nav .st i{width:7px;height:7px;border-radius:50%;background:#9a9992;display:inline-block}
.centen-nav .st .ok i{background:#0a7d0a}.centen-nav .st .stale i{background:#d8902a}.centen-nav .st .error i{background:#c62f2e}
.centen-nav .st .off{display:none}
.centen-nav .short{display:none}
.centen-warn{background:#fdf1dc;color:#5a3a00;padding:6px 16px;font:13px system-ui,sans-serif}
@media (max-width:700px){.centen-nav{padding:6px 8px;gap:2px}.centen-nav b{display:none}
.centen-nav a{padding:6px 8px}.centen-nav .long{display:none}.centen-nav .short{display:inline}
.centen-nav .st{gap:6px;padding:4px}.centen-nav .st em{display:none}}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]) .centen-nav{background:#1d1d1b;border-color:#383835}
:root:not([data-theme="light"]) .centen-nav b{color:#f0efec}
:root:not([data-theme="light"]) .centen-nav a{color:#e6e5df}
:root:not([data-theme="light"]) .centen-nav .st{color:#a9a8a1}
:root:not([data-theme="light"]) .centen-nav a:hover{background:#2c2c2a;color:#ffffff}
:root:not([data-theme="light"]) .centen-nav a[aria-current]{background:#f0efec;color:#141413}}
:root[data-theme="dark"] .centen-nav{background:#1d1d1b;border-color:#383835}
:root[data-theme="dark"] .centen-nav b{color:#f0efec}
:root[data-theme="dark"] .centen-nav a{color:#e6e5df}
:root[data-theme="dark"] .centen-nav .st{color:#a9a8a1}
:root[data-theme="dark"] .centen-nav a:hover{background:#2c2c2a;color:#ffffff}
:root[data-theme="dark"] .centen-nav a[aria-current]{background:#f0efec;color:#141413}
</style><nav class="centen-nav" aria-label="centen"><b>centen</b><a href="/" __A_/__>Dashboard</a>
<a href="/bijwerken" __A_/bijwerken__>Bijwerken</a><a href="/digest" __A_/digest__><span class="long">Weekoverzicht</span><span class="short">Week</span></a>
<span class="sp"></span>__STATUS__<a href="/logout" title="Afmelden" aria-label="Afmelden"><span class="long">Afmelden</span><span class="short">⏻</span></a></nav>__WARN__"""

_FEED_NL = {"bank": "Bank", "prices": "Koersen", "crypto": "Crypto"}
_STATE_NL = {"ok": "bijgewerkt", "stale": "niet recent bijgewerkt", "error": "fout", "off": "niet ingesteld"}


def _esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def status_html(h: dict | None) -> str:
    """Three dots in the nav: bank, prices, crypto (green ok, orange stale, red error). Click: the to-do list."""
    if not h:
        return ""
    parts = []
    for k, f in h["feeds"].items():
        at = f.get("at")
        when = ""
        if at:
            d = datetime.fromisoformat(at)
            when = d.strftime("%H:%M") if d.date() == date.today() else d.strftime("%d/%m")
        tip = f"{_FEED_NL[k]}: {_STATE_NL[f['state']]}" + (f" ({at.replace('T', ' ')})" if at else "")
        parts.append(f'<span class="{f["state"]}" title="{_esc(tip)}"><i></i><em>{_FEED_NL[k]} {when}</em></span>')
    n = len(h.get("issues") or [])
    tip = f"{n} punt(en) in Te doen" if n else "Alles bijgewerkt"
    return f'<a class="st" href="/bijwerken" title="{_esc(tip)}" aria-label="Status: {_esc(tip)}">{"".join(parts)}</a>'


def nav(current: str, health: dict | None = None, warn: str = "") -> str:
    out = NAV
    for p in ("/", "/bijwerken", "/digest"):
        out = out.replace(f"__A_{p}__", 'aria-current="page"' if p == current else "")
    out = out.replace("__STATUS__", status_html(health))
    return out.replace("__WARN__", f'<div class="centen-warn" role="alert">{_esc(warn)}</div>' if warn else "")


CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
       "connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")


def build_app(key: str | None = None, run_scheduler: bool = True):
    from starlette.applications import Starlette
    from starlette.concurrency import run_in_threadpool
    from starlette.requests import Request
    from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
    from starlette.routing import Mount, Route

    from . import app as upd
    from . import health as H
    from . import jobs, pwa
    from .server import server as mcp

    upd.WHERE = "server"
    key = key or access_key()
    weak = "" if len(key) >= MIN_KEY else (f"De toegangssleutel is korter dan {MIN_KEY} tekens. Zet een lange willekeurige "
                                           "CENTEN_KEY in .env (bv. de uitvoer van: openssl rand -base64 30).")
    mcp_app = mcp.streamable_http_app(streamable_http_path="/mcp", json_response=True, stateless_http=True,
                                      host="0.0.0.0")
    limiter = LoginLimiter()
    dash = DashboardBuilder()

    def _sid(req: Request) -> str | None:
        return req.cookies.get(COOKIE)

    def _logged_in(req: Request) -> bool:
        with closing(db.connect()) as c:
            return session_check(c, key, _sid(req))

    async def guard(req: Request, api: bool = False):
        if not await run_in_threadpool(_logged_in, req):
            return JSONResponse({"error": "niet aangemeld"}, 401) if api else RedirectResponse("/login", 303)
        if api and not hmac.compare_digest(req.headers.get("x-centen-key", ""), _csrf(key, _sid(req) or "")):
            return JSONResponse({"error": "forbidden"}, 403)
        return None

    def secure_headers(r: Response) -> Response:
        r.headers["X-Frame-Options"] = "DENY"
        r.headers["Cache-Control"] = "no-store"
        r.headers["Referrer-Policy"] = "no-referrer"
        r.headers["X-Content-Type-Options"] = "nosniff"
        r.headers["Content-Security-Policy"] = CSP
        return r

    def _ip(req: Request) -> str:
        return req.client.host if req.client else "?"

    async def login(req: Request):
        if req.method == "GET":
            return secure_headers(HTMLResponse(LOGIN.replace("__ERR__", "")))
        ip = _ip(req)
        if limiter.blocked(ip):
            return secure_headers(HTMLResponse(LOGIN.replace(
                "__ERR__", "<p class=e>Te veel pogingen. Probeer het over tien minuten opnieuw.</p>"), 429))
        form = await req.form()
        if hmac.compare_digest(str(form.get("key", "")).strip().encode(), key.encode()):
            limiter.ok(ip)

            def _new():
                with closing(db.connect()) as c:
                    return session_new(c, key, req.headers.get("user-agent"))
            sid = await run_in_threadpool(_new)
            r = RedirectResponse("/", 303)
            r.set_cookie(COOKIE, sid, httponly=True, samesite="strict", max_age=60 * 60 * 24 * SESSION_DAYS,
                         secure=req.url.scheme == "https")
            return r
        limiter.fail(ip)
        await asyncio.sleep(1)
        return secure_headers(HTMLResponse(LOGIN.replace("__ERR__", "<p class=e>Verkeerde sleutel.</p>"), 401))

    async def logout(req: Request):
        sid = _sid(req)

        def _end():
            with closing(db.connect()) as c:
                session_end(c, sid)
        await run_in_threadpool(_end)
        r = RedirectResponse("/login", 303)
        r.delete_cookie(COOKIE)
        r.headers["Clear-Site-Data"] = '"cache"'  # the phone's offline copy of the dashboard goes too
        return r

    def _health():
        try:
            with closing(db.connect()) as c:
                return H.summary(c)
        except Exception as e:  # noqa: BLE001
            log.warning("health: %s", e)
            return None

    def _page(html: str, current: str) -> str:
        return pwa.inject(html.replace("<body>", "<body>" + nav(current, _health(), weak), 1))

    async def index(req: Request):
        if (g := await guard(req)):
            return g

        def _render():
            return _page(dash.current().read_text(encoding="utf-8"), "/")
        return secure_headers(HTMLResponse(await run_in_threadpool(_render)))

    async def update_page(req: Request):
        if (g := await guard(req)):
            return g
        token = _csrf(key, _sid(req) or "")

        def _render():
            return _page(upd.TEMPLATE.read_text(encoding="utf-8").replace("__TOKEN__", token), "/bijwerken")
        return secure_headers(HTMLResponse(await run_in_threadpool(_render)))

    async def digest_page(req: Request):
        if (g := await guard(req)):
            return g

        def _render():
            p = config.home() / "digest.html"
            if not p.exists():
                with closing(db.connect()) as c:
                    jobs._digest(c)
            return _page(p.read_text(encoding="utf-8"), "/digest")
        return secure_headers(HTMLResponse(await run_in_threadpool(_render)))

    async def api_state(req: Request):
        if (g := await guard(req, api=True)):
            return g
        sid = _sid(req)

        def _state():
            with closing(db.connect()) as c:
                st = upd.state(c)
                st["sessions"] = sessions(c, sid)
                return json.loads(json.dumps(st, default=str))
        return secure_headers(JSONResponse(await run_in_threadpool(_state)))

    async def api_act(req: Request):
        if (g := await guard(req, api=True)):
            return g
        action, sid = req.path_params["action"], _sid(req)
        try:
            body = await req.json()

            def _act():
                with closing(db.connect()) as c:
                    if action == "logout_others":
                        return {"removed": session_end(c, sid, others=True)}
                    return upd.act(c, action, body)
            res = await run_in_threadpool(_act)
            if action != "logout_others":
                dash.mark_dirty()
            return JSONResponse({"ok": True, "result": json.loads(json.dumps(res, default=str))})
        except (ValueError, KeyError) as e:
            return JSONResponse({"error": str(e)}, 400)

    async def api_upload(req: Request):
        if (g := await guard(req, api=True)):
            return g
        data = await req.body()
        if len(data) > 30 * 1024 * 1024:
            return JSONResponse({"error": "Bestand te groot (max 30 MB)."}, 413)
        q = req.query_params
        try:
            def _up():
                with closing(db.connect()) as c:
                    return upd.upload(c, q.get("kind", "bank"), q.get("account"), q.get("name", "export.csv"), data)
            res = await run_in_threadpool(_up)
            dash.mark_dirty()
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
                auth = dict(scope.get("headers") or []).get(b"authorization", b"")
                if not hmac.compare_digest(auth, f"Bearer {key}".encode()):
                    await JSONResponse({"error": "unauthorized"}, 401)(scope, receive, send)
                    return
            await self.inner(scope, receive, send)

    stop = threading.Event()

    @contextlib.asynccontextmanager
    async def lifespan(app):
        dash.mark_dirty()  # a new version may change the dashboard's template: rebuild once at start
        if run_scheduler:
            threading.Thread(target=jobs.scheduler, args=(stop, dash.mark_dirty), daemon=True,
                             name="centen-scheduler").start()
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
    app = Starlette(routes=routes, lifespan=lifespan)
    app.state.dashboard = dash
    return app


def serve(host: str = "0.0.0.0", port: int = 8770, run_scheduler: bool = True):
    import uvicorn
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    with closing(db.connect()):
        pass  # create / migrate the database before the first request
    key = access_key()
    log.info("centen web on http://%s:%s  (dashboard /, update /bijwerken, MCP /mcp)", host, port)
    if not os.environ.get("CENTEN_KEY"):
        log.info("access key in %s", config.home() / "web_key")
    if len(key) < MIN_KEY:
        log.warning("the access key is shorter than %s characters: set a long random CENTEN_KEY", MIN_KEY)
    uvicorn.run(build_app(key, run_scheduler), host=host, port=port, log_level="warning", proxy_headers=True)
