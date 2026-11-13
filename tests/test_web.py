"""Server mode: login, the update API behind a CSRF header, MCP over HTTP with a Bearer key, and the stdio relay."""
import io
import json

import pytest

starlette = pytest.importorskip("starlette")
from starlette.testclient import TestClient  # noqa: E402

from centen import remote, web  # noqa: E402

INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}
H = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


@pytest.fixture()
def client(demo_conn):
    app = web.build_app("k3y", run_scheduler=False)
    with TestClient(app, base_url="http://nas.local") as c:
        yield c


def test_login_required_and_works(client):
    assert client.get("/", follow_redirects=False).status_code == 303
    assert client.post("/login", data={"key": "wrong"}).status_code == 401
    r = client.post("/login", data={"key": "k3y"}, follow_redirects=False)
    assert r.status_code == 303 and web.COOKIE in r.cookies
    page = client.get("/")
    assert page.status_code == 200 and "centen-data" in page.text and page.headers["x-frame-options"] == "DENY"


def test_update_api_needs_session_and_csrf(client):
    assert client.get("/api/state").status_code == 401
    client.post("/login", data={"key": "k3y"})
    assert client.get("/api/state").status_code == 403  # no CSRF header
    csrf = web._csrf("k3y", client.cookies.get(web.COOKIE))
    assert "__TOKEN__" not in client.get("/bijwerken").text and csrf in client.get("/bijwerken").text
    assert client.get("/api/state", headers={"X-Centen-Key": web._csrf("k3y", "other-session")}).status_code == 403
    st = client.get("/api/state", headers={"X-Centen-Key": csrf}).json()
    assert "savings" in st
    r = client.post("/api/act/nothing", json={}, headers={"X-Centen-Key": csrf})
    assert r.status_code == 400


def test_mcp_over_http_needs_bearer(client):
    assert client.post("/mcp", json=INIT, headers=H).status_code == 401
    r = client.post("/mcp", json=INIT, headers={**H, "Authorization": "Bearer k3y"})
    assert r.status_code == 200 and r.json()["result"]["serverInfo"]["name"] == "centen"
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                    headers={**H, "Authorization": "Bearer k3y"})
    assert "tax_overview" in {t["name"] for t in r.json()["result"]["tools"]}


def test_remote_relay(client):
    out = io.StringIO()
    lines = "\n".join(json.dumps(m) for m in (INIT, {"jsonrpc": "2.0", "method": "notifications/initialized"},
                                               {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}))
    remote.relay("http://nas.local/mcp", "k3y", io.StringIO(lines), out, client=client)
    answers = [json.loads(x) for x in out.getvalue().splitlines()]
    assert [a["id"] for a in answers] == [1, 2] and answers[1]["result"]["tools"]
    out = io.StringIO()
    remote.relay("http://nas.local/mcp", "wrong", io.StringIO(json.dumps(INIT)), out, client=client)
    assert json.loads(out.getvalue())["error"]["code"] == -32000


def test_access_key_is_generated_once(tmp_path, monkeypatch):
    monkeypatch.delenv("CENTEN_KEY", raising=False)
    monkeypatch.setenv("CENTEN_HOME", str(tmp_path))
    k = web.access_key()
    assert len(k) >= 24 and web.access_key() == k and (tmp_path / "web_key").read_text() == k
    monkeypatch.setenv("CENTEN_KEY", "fromenv")
    assert web.access_key() == "fromenv"


def test_nav_is_themed_and_marks_current_page():
    from centen import web
    html = web.nav("/bijwerken")
    assert 'class="centen-nav"' in html
    assert '[data-theme="dark"] .centen-nav' in html and "prefers-color-scheme: dark" in html
    assert 'href="/bijwerken" aria-current="page"' in html
    assert 'href="/" aria-current' not in html and "__A_" not in html


def test_remote_relay_output_survives_a_cp1252_console():
    import io
    import httpx
    from centen import remote
    def handler(request):
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"t": "− 0,13% €"}})
    out = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
    remote.relay("http://x/mcp", "k", stdin=io.StringIO('{"jsonrpc":"2.0","id":1,"method":"ping"}\n'), stdout=out,
                 client=httpx.Client(transport=httpx.MockTransport(handler)))
    out.seek(0)
    import json
    assert json.loads(out.read())["result"]["t"] == "− 0,13% €"


def test_phone_app_files_and_offline_cache(client):
    m = client.get("/manifest.webmanifest")
    assert m.status_code == 200 and m.json()["display"] == "standalone" and "manifest+json" in m.headers["content-type"]
    sw = client.get("/sw.js")
    assert sw.status_code == 200 and "javascript" in sw.headers["content-type"] and "/api" in sw.text
    for name in ("icon-192.png", "icon-512.png", "icon-maskable-512.png", "apple-touch-icon.png"):
        r = client.get("/" + name)
        assert r.status_code == 200 and r.headers["content-type"] == "image/png" and r.content[:4] == b"\x89PNG"
    client.post("/login", data={"key": "k3y"})
    page = client.get("/")
    assert '<link rel="manifest"' in page.text and "serviceWorker" in page.text
    out = client.get("/logout", follow_redirects=False)
    assert out.headers["clear-site-data"] == '"cache"'


def test_scheduler_alert_job_skips_without_ntfy(demo_conn):
    assert web._alerts(demo_conn) == {"skipped": "notifications not configured"}


def test_logout_really_ends_the_session(client):
    client.post("/login", data={"key": "k3y"})
    sid = client.cookies.get(web.COOKIE)
    assert client.get("/", follow_redirects=False).status_code == 200
    client.get("/logout", follow_redirects=False)
    client.cookies.clear()
    client.cookies.set(web.COOKIE, sid)  # an old copy of the cookie no longer works
    assert client.get("/", follow_redirects=False).status_code == 303


def test_logout_other_devices_and_key_change(demo_conn):
    K = "k3y-long-enough-for-the-check"
    app = web.build_app(K, run_scheduler=False)
    with TestClient(app, base_url="http://nas.local") as c:
        c.post("/login", data={"key": K}, headers={"User-Agent": "Mozilla (Windows) Chrome/1 Safari/1"})
        phone = c.cookies.get(web.COOKIE)
        c.cookies.clear()
        c.post("/login", data={"key": K}, headers={"User-Agent": "Mozilla (iPhone) Safari/1"})
        laptop = c.cookies.get(web.COOKIE)
        csrf = web._csrf(K, laptop)
        st = c.get("/api/state", headers={"X-Centen-Key": csrf}).json()
        assert len(st["sessions"]) == 2 and sum(x["current"] for x in st["sessions"]) == 1
        assert {x["agent"] for x in st["sessions"]} == {"iPhone · Safari", "Windows · Chrome"} and st["where"] == "server"
        r = c.post("/api/act/logout_others", json={}, headers={"X-Centen-Key": csrf})
        assert r.json()["result"]["removed"] == 1
        assert c.get("/", follow_redirects=False).status_code == 200
        c.cookies.clear()
        c.cookies.set(web.COOKIE, phone)
        assert c.get("/", follow_redirects=False).status_code == 303
    from contextlib import closing
    from centen import db
    with closing(db.connect()) as conn:
        assert web.session_check(conn, K, laptop) and not web.session_check(conn, "a-completely-different-key", laptop)


def test_login_is_rate_limited(client):
    for _ in range(5):
        assert client.post("/login", data={"key": "nope"}).status_code == 401
    assert client.post("/login", data={"key": "k3y"}).status_code == 429


def test_pages_have_security_headers_and_status_bar(client):
    client.post("/login", data={"key": "k3y"})
    for path in ("/", "/bijwerken"):
        r = client.get(path)
        assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
        assert r.headers["x-content-type-options"] == "nosniff"
        assert 'class="st"' in r.text and "Koersen" in r.text
    assert "korter dan 20 tekens" in client.get("/").text  # "k3y" is a weak key: warned on every page
    assert 'class="centen-warn"' not in web.nav("/", None, "")


def test_saving_marks_the_dashboard_for_a_background_rebuild(client):
    client.post("/login", data={"key": "k3y"})
    client.get("/")
    dash = client.app.state.dashboard
    csrf = web._csrf("k3y", client.cookies.get(web.COOKIE))
    acc = client.get("/api/state", headers={"X-Centen-Key": csrf}).json()["savings"]
    if acc:
        r = client.post("/api/act/savings_balance", json={"account": acc[0]["name"], "amount": "1.234"},
                        headers={"X-Centen-Key": csrf})
        assert r.status_code == 200 and dash.dirty.is_set()
        dash._timer.cancel()
        dash.build_now()
        assert not dash.dirty.is_set()
