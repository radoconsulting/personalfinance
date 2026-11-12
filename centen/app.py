"""'centen bijwerken': a small local web app to update the figures no bank link or price feed delivers — savings
balances, values of pension and insurance contracts and other accounts, the home's value, loan statements,
contributions, missing 31/12/2025 prices, labels for 'Other', allocation targets, new accounts, and file imports
(broker exports, older bank history).

It runs on this computer only (127.0.0.1), with a random key in the address that every change must carry, and
writes to the same database as Claude and the dashboard (which is rebuilt after each change).

Start: `centen app` (opens the browser), or ask Claude (`open_update_app`).
"""
from __future__ import annotations

import json
import re
import secrets
import threading
import webbrowser
from contextlib import closing
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import config, db

PORT = 8770
TEMPLATE = Path(__file__).with_name("app_template.html")
_server: ThreadingHTTPServer | None = None
_url: str | None = None


# --- helpers ------------------------------------------------------------------------------------

def amount(v) -> float:
    """'40.000', '1.234,56', '1234.56', 1234.5 -> float (Belgian notation understood)."""
    if isinstance(v, (int, float)):
        return float(v)
    from .money import parse_amount
    t = str(v or "").strip().replace("€", "").replace(" ", "").replace(" ", "")
    if re.fullmatch(r"-?\d{1,3}(\.\d{3})+", t):
        t = t.replace(".", "")
    x = parse_amount(t)
    if x is None:
        raise ValueError(f"Geen bedrag: {v!r}")
    return float(x)


def day(v) -> str:
    if not v:
        return date.today().isoformat()
    s = str(v).strip()
    m = re.fullmatch(r"(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})", s)
    if m:
        s = f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
    date.fromisoformat(s[:10])
    return s[:10]


def _refresh(conn):
    from . import dashboard
    dashboard.refresh(conn)


# --- state ----------------------------------------------------------------------------------------

def state(conn, today: date | None = None) -> dict:
    from . import analysis, assets, loans, manual, review, tax
    from .categorize import CATEGORIES
    today = today or date.today()
    out = {"today": today.isoformat(), "generated": datetime.now().strftime("%H:%M")}
    out["savings"] = manual.list_accounts(conn)
    accs = assets.list_accounts(conn, today)
    plans = {x["account"]: x for x in loans.all_loans(conn, today)}
    out["assets"] = []
    for a in accs:
        item = {k: a.get(k) for k in ("name", "type", "type_label", "provider", "scope", "value", "value_entered",
                                      "stale", "days_since_value", "estimated", "contributions",
                                      "contributions_entered", "outstanding", "positions")}
        item["has_trades"] = bool(conn.execute(
            "SELECT 1 FROM inv_transactions t JOIN accounts x ON x.id=t.account_id WHERE x.name=? AND x.source='asset' "
            "LIMIT 1", (a["name"],)).fetchone())
        item["live_priced"] = a["type"] in ("investment", "crypto") and (item["has_trades"] or bool(a.get("positions")))
        if a["type"] == "loan":
            item["plan"] = plans.get(a["name"])
        out["assets"].append(item)
    try:
        cg = tax.capital_gains(conn, today)
        out["missing_photo"] = [{"instrument": p["instrument"], "key": p["key"], "account": p["account"]}
                                for p in cg["latent"] if p["photo_price"] is None and p["oldest_lot"] < "2026"]
    except Exception:  # noqa: BLE001
        out["missing_photo"] = []
    out["review"] = {}
    for scope in ("personal", "business"):
        q = review.queue(conn, scope, 40, date_from=date(today.year - 1, 1, 1).isoformat(), today=today)
        for i in q["items"]:
            i.pop("transaction_ids", None)
        out["review"][scope] = q
    out["categories"] = [c[0] for c in CATEGORIES if c[0] not in ("Other expenses", "Other income")]
    out["targets"] = analysis.targets(conn)
    out["imports_folder"] = str(config.home().parent / "imports")
    out["broker_accounts"] = [a["name"] for a in accs if a["type"] in ("investment", "crypto")]
    return out


# --- actions --------------------------------------------------------------------------------------

def act(conn, action: str, p: dict) -> dict:
    from . import analysis, assets, history, loans, manual, review
    if action == "savings_balance":
        return manual.set_balance(conn, p["account"], amount(p["amount"]), day(p.get("date")))
    if action == "asset_value":
        return assets.set_value(conn, p["account"], amount(p["value"]), day(p.get("date")))
    if action == "contributions":
        return assets.update_account(conn, p["account"], contributions=amount(p["amount"]),
                                     contributions_date=day(p.get("date")))
    if action == "loan_statement":
        return loans.new_statement(conn, p["account"], amount(p["outstanding"]), day(p.get("date")))
    if action == "reference_price":
        key = str(p["instrument"]).strip().upper()
        return history.set_reference(conn, key, amount(p["price"]))
    if action == "label":
        return review.label(conn, p["merchant"], p["category"], p.get("scope") or None)
    if action == "targets":
        vals = {k: amount(v) for k, v in (p.get("targets") or {}).items() if str(v).strip()}
        return analysis.set_targets(conn, p["dimension"], vals)
    if action == "add_asset":
        return assets.add_account(conn, p["name"], p["type"], amount(p["value"]) if p.get("value") else None,
                                  day(p.get("date")), p.get("provider") or None, p.get("scope") or None,
                                  [m.strip() for m in str(p.get("match") or "").split(",") if m.strip()] or None)
    raise ValueError(f"Onbekende actie {action}")


def upload(conn, kind: str, account: str | None, filename: str, data: bytes) -> dict:
    """Save an uploaded export under imports/uploads/<today>/ and import it."""
    safe = re.sub(r"[^A-Za-z0-9._ -]+", "_", Path(filename).name)[:120] or "export"
    if not re.search(r"\.(csv|txt|pdf|xml|cod|coda)$", safe, re.I):
        raise ValueError("Verwacht een CSV-, PDF-, XML- of CODA-bestand.")
    folder = config.home().parent / "imports" / "uploads" / date.today().isoformat()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / safe
    path.write_bytes(data)
    if kind == "broker":
        from . import portfolio
        if not account:
            raise ValueError("Kies de rekening waartoe de export hoort.")
        res = portfolio.import_files(conn, account, [str(path)])
    else:
        from .importers import import_history
        res = import_history(conn, [str(path)])
    return {"saved_as": str(path), "result": res}


# --- web server ---------------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    token = ""
    port = PORT

    def log_message(self, *a):  # quiet
        pass

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").split(":")[0]
        return host in ("127.0.0.1", "localhost")

    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'self' 'unsafe-inline'; img-src 'self' data:")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"), "application/json")

    def do_GET(self):
        u = urlparse(self.path)
        if not self._host_ok():
            return self._send(403, b"forbidden", "text/plain")
        q = parse_qs(u.query)
        if u.path == "/":
            if q.get("k", [""])[0] != self.token:
                return self._send(403, "Open de app via `centen app` of vraag het aan Claude.".encode("utf-8"),
                                  "text/plain; charset=utf-8")
            html = TEMPLATE.read_text(encoding="utf-8").replace("__TOKEN__", self.token)
            return self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
        if u.path == "/api/state":
            if self.headers.get("X-Centen-Key") != self.token:
                return self._json(403, {"error": "forbidden"})
            with closing(db.connect()) as c:
                return self._json(200, state(c))
        return self._send(404, b"not found", "text/plain")

    def do_POST(self):
        u = urlparse(self.path)
        if not self._host_ok() or self.headers.get("X-Centen-Key") != self.token:
            return self._json(403, {"error": "forbidden"})
        n = int(self.headers.get("Content-Length") or 0)
        if n > 30 * 1024 * 1024:
            return self._json(413, {"error": "Bestand te groot (max 30 MB)."})
        body = self.rfile.read(n)
        try:
            with closing(db.connect()) as c:
                if u.path == "/api/upload":
                    q = parse_qs(u.query)
                    res = upload(c, q.get("kind", ["bank"])[0], (q.get("account") or [None])[0],
                                 q.get("name", ["export.csv"])[0], body)
                else:
                    m = re.fullmatch(r"/api/act/([a-z_]+)", u.path)
                    if not m:
                        return self._json(404, {"error": "not found"})
                    res = act(c, m.group(1), json.loads(body or b"{}"))
                _refresh(c)
            return self._json(200, {"ok": True, "result": res})
        except (ValueError, KeyError, FileNotFoundError) as e:
            return self._json(400, {"error": str(e)})
        except Exception as e:  # noqa: BLE001
            return self._json(500, {"error": f"{type(e).__name__}: {e}"})


def start(port: int = PORT, open_browser: bool = True, block: bool = False) -> str:
    """Start the app (once per process) and return its address."""
    global _server, _url
    if _server is None:
        token = secrets.token_urlsafe(18)
        handler = type("CentenHandler", (Handler,), {"token": token, "port": port})
        for p in range(port, port + 10):
            try:
                _server = ThreadingHTTPServer(("127.0.0.1", p), handler)
                break
            except OSError:
                continue
        if _server is None:
            raise OSError(f"Geen vrije poort tussen {port} en {port + 9}.")
        _url = f"http://127.0.0.1:{_server.server_address[1]}/?k={token}"
        if not block:
            threading.Thread(target=_server.serve_forever, daemon=True).start()
    if open_browser:
        try:
            webbrowser.open(_url)
        except Exception:  # noqa: BLE001
            pass
    if block:
        print(f"centen bijwerken draait op {_url}  (Ctrl+C om te stoppen)")
        try:
            _server.serve_forever()
        except KeyboardInterrupt:
            pass
    return _url
