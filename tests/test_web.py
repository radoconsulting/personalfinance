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
    csrf = web._csrf("k3y")
    assert "__TOKEN__" not in client.get("/bijwerken").text and csrf in client.get("/bijwerken").text
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
