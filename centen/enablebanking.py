"""Read-only PSD2 account access through Enable Banking (https://enablebanking.com).

Enable Banking's "restricted production" mode is free for private individuals reading their own accounts:
register a production application, activate it by linking your own accounts in their Control Panel, then
authorise access through the API (this module). Only account-information endpoints are used; there is no
code path that initiates payments.
"""
from __future__ import annotations

import secrets
import time
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import jwt

from . import db
from .categorize import Context, enrich, recategorize
from .importers import account_id_for
from .money import clean_iban, parse_date


class EnableBankingError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(f"Enable Banking API error {status}: {message}")
        self.status = status


class Client:
    def __init__(self, app_id: str, private_key_pem: str | bytes, api_base: str = "https://api.enablebanking.com",
                 transport: httpx.BaseTransport | None = None, timeout: float = 60.0):
        if not app_id or not private_key_pem:
            raise ValueError("Enable Banking app_id and private key are required (run `centen init`).")
        self.app_id = app_id
        self.key = private_key_pem
        self.base = api_base.rstrip("/")
        self._token: tuple[str, float] | None = None
        self.http = httpx.Client(base_url=self.base, timeout=timeout, transport=transport)

    @classmethod
    def from_config(cls, cfg: dict, transport=None) -> "Client":
        eb = cfg["enablebanking"]
        key_path = Path(eb.get("private_key_path") or "").expanduser()
        if not key_path.is_file() and eb.get("private_key_path"):
            # moved to another machine (e.g. a NAS container): look for the same file in the data folder
            from . import config as _cfg
            name = str(eb["private_key_path"]).replace("\\", "/").rsplit("/", 1)[-1]
            if (_cfg.home() / name).is_file():
                key_path = _cfg.home() / name
        if not key_path.is_file():
            raise ValueError(f"Private key not found at '{key_path}'. Run `centen init --key /path/to/key.pem`.")
        return cls(eb["app_id"], key_path.read_bytes(), eb.get("api_base") or "https://api.enablebanking.com",
                   transport=transport)

    def _jwt(self) -> str:
        now = int(time.time())
        if self._token and self._token[1] - 60 > now:
            return self._token[0]
        token = jwt.encode({"iss": "enablebanking.com", "aud": "api.enablebanking.com", "iat": now, "exp": now + 3600},
                           self.key, algorithm="RS256", headers={"kid": self.app_id})
        self._token = (token, now + 3600)
        return token

    def _req(self, method: str, path: str, **kw) -> dict:
        r = self.http.request(method, path, headers={"Authorization": f"Bearer {self._jwt()}"}, **kw)
        if r.status_code >= 400:
            try:
                body = r.json()
                msg = body.get("message") or body.get("detail") or str(body)
            except ValueError:
                msg = r.text[:300]
            raise EnableBankingError(r.status_code, str(msg)[:500])
        return r.json() if r.content else {}

    # --- endpoints ---------------------------------------------------------------
    def application(self) -> dict:
        return self._req("GET", "/application")

    def aspsps(self, country: str = "BE", psu_type: str = "personal") -> list[dict]:
        return self._req("GET", "/aspsps", params={"country": country, "psu_type": psu_type}).get("aspsps", [])

    def start_auth(self, aspsp_name: str, country: str, redirect_url: str, valid_until: datetime, state: str,
                   psu_type: str = "personal") -> dict:
        body = {"access": {"valid_until": valid_until.astimezone(timezone.utc).isoformat()},
                "aspsp": {"name": aspsp_name, "country": country}, "state": state,
                "redirect_url": redirect_url, "psu_type": psu_type}
        return self._req("POST", "/auth", json=body)

    def create_session(self, code: str) -> dict:
        return self._req("POST", "/sessions", json={"code": code})

    def get_session(self, session_id: str) -> dict:
        return self._req("GET", f"/sessions/{session_id}")

    def delete_session(self, session_id: str) -> dict:
        return self._req("DELETE", f"/sessions/{session_id}")

    def balances(self, account_uid: str) -> list[dict]:
        return self._req("GET", f"/accounts/{account_uid}/balances").get("balances", [])

    def transactions(self, account_uid: str, date_from: date, date_to: date | None = None, max_pages: int = 200):
        params = {"date_from": date_from.isoformat()}
        if date_to:
            params["date_to"] = date_to.isoformat()
        for _ in range(max_pages):
            page = self._req("GET", f"/accounts/{account_uid}/transactions", params=params)
            yield from page.get("transactions", []) or []
            key = page.get("continuation_key")
            if not key:
                return
            params = {**params, "continuation_key": key}


# --- linking ---------------------------------------------------------------------

def find_aspsp(client: Client, name: str, country: str = "BE") -> dict:
    banks = client.aspsps(country)
    exact = [b for b in banks if b["name"].lower() == name.lower()]
    if exact:
        return exact[0]
    partial = [b for b in banks if name.lower() in b["name"].lower()]
    if len(partial) == 1:
        return partial[0]
    names = ", ".join(sorted(b["name"] for b in (partial or banks)))
    raise ValueError(f"Bank '{name}' not found unambiguously in {country}. Options: {names}")


def start_link(conn, cfg: dict, bank_name: str, country: str | None = None, client: Client | None = None) -> dict:
    client = client or Client.from_config(cfg)
    country = country or cfg["enablebanking"].get("country", "BE")
    aspsp = find_aspsp(client, bank_name, country)
    max_secs = int(aspsp.get("maximum_consent_validity") or 90 * 86400)
    valid_until = datetime.now(timezone.utc) + timedelta(seconds=min(max_secs, 180 * 86400) - 3600)
    state = secrets.token_urlsafe(24)
    auth = client.start_auth(aspsp["name"], country, cfg["enablebanking"]["redirect_url"], valid_until, state)
    db.set_meta(conn, f"pending_state:{state}", f"{aspsp['name']}|{country}|{valid_until.isoformat()}")
    conn.commit()
    return {"url": auth["url"], "state": state, "bank": aspsp["name"], "valid_until": valid_until.isoformat()}


def code_from_redirect(redirect: str) -> tuple[str, str | None]:
    """Accepts the full redirected URL (…?state=…&code=…) or a bare code."""
    redirect = redirect.strip()
    if "code=" in redirect or "error=" in redirect:
        q = parse_qs(urlparse(redirect).query)
        if "error" in q:
            raise ValueError(f"Bank authorisation failed: {q.get('error')} {q.get('error_description', '')}")
        return q["code"][0], (q.get("state") or [None])[0]
    return redirect, None


def _account_type(a: dict) -> str:
    cat = (a.get("cash_account_type") or "").upper()
    hint = " ".join(str(a.get(k) or "") for k in ("name", "product", "usage", "details")).lower()
    if cat == "CARD" or any(w in hint for w in ("visa", "mastercard", "kredietkaart", "credit card", "carte de credit")):
        return "card"
    if cat == "SVGS" or any(w in hint for w in ("spaar", "epargne", "savings", "sparen")):
        return "savings"
    if cat in ("LOAN", "MORT"):
        return "loan"
    return "current"


def map_account(a: dict, link_id: str, bank: str) -> dict:
    iban = clean_iban((a.get("account_id") or {}).get("iban"))
    stable = a.get("identification_hash") or a.get("uid")
    limit = a.get("credit_limit") or {}
    name = a.get("name") or a.get("product") or a.get("details")
    return {
        "id": account_id_for(iban, f"eb:{stable}"), "source": "enablebanking", "bank": bank, "iban": iban,
        "name": name or (f"{bank} …{iban[-4:]}" if iban else bank), "currency": a.get("currency") or "EUR",
        "account_type": _account_type(a), "credit_limit": float(limit["amount"]) if limit.get("amount") else None,
        "external_uid": a.get("uid"), "link_id": link_id,
    }


def complete_link(conn, cfg: dict, redirect_or_code: str, client: Client | None = None) -> dict:
    client = client or Client.from_config(cfg)
    code, state = code_from_redirect(redirect_or_code)
    pending = None
    if state:
        pending = db.get_meta(conn, f"pending_state:{state}")
        if pending is None:
            raise ValueError("Unknown or already used 'state' in the redirect URL. Start again with `centen link`.")
    else:
        row = conn.execute("SELECT key, value FROM meta WHERE key LIKE 'pending_state:%' ORDER BY rowid DESC LIMIT 1").fetchone()
        if row:
            state, pending = row[0].split(":", 1)[1], row[1]
    session = client.create_session(code)
    bank, country, valid_until = (pending.split("|") if pending else
                                  [session.get("aspsp", {}).get("name"), session.get("aspsp", {}).get("country"), None])
    valid_until = (session.get("access") or {}).get("valid_until") or valid_until
    link_id = session["session_id"]
    conn.execute("INSERT OR REPLACE INTO bank_links(id, aspsp_name, country, session_id, valid_until, status) "
                 "VALUES (?,?,?,?,?, 'active')", (link_id, bank, country, session["session_id"], valid_until))
    accounts = [map_account(a, link_id, bank) for a in session.get("accounts", [])]
    for acc in accounts:
        db.upsert_account(conn, **acc)
    if state:
        conn.execute("DELETE FROM meta WHERE key=?", (f"pending_state:{state}",))
    db._replaced_links(conn)  # linking a bank again: the old link's accounts moved to this one
    conn.commit()
    recategorize(conn)  # new own accounts change how earlier transfers are classified
    return {"link_id": link_id, "bank": bank, "valid_until": valid_until,
            "accounts": [{"name": a["name"], "type": a["account_type"], "iban": db.mask_iban(a["iban"])} for a in accounts]}


# --- sync ------------------------------------------------------------------------

_BOOKED = ("CLBD", "ITBD", "OPBD", "PRCD", "INFO", "OTHR")
_AVAILABLE = ("CLAV", "ITAV", "XPCD", "FWAV", "ITAV")


def _pick(balances: list[dict], order) -> dict | None:
    for code in order:
        for b in balances:
            if (b.get("balance_type") or "").upper() == code:
                return b
    return None


def map_transaction(t: dict, account_id: str, occurrence: int) -> dict | None:
    status = (t.get("status") or "BOOK").upper()
    if status in ("CNCL", "RJCT"):
        return None
    amt = float((t.get("transaction_amount") or {}).get("amount") or 0)
    amount = -abs(amt) if (t.get("credit_debit_indicator") or "").upper() == "DBIT" else abs(amt)
    party, party_acc = ("creditor", "creditor_account") if amount < 0 else ("debtor", "debtor_account")
    cp = t.get(party) or {}
    cp_acc = t.get(party_acc) or {}
    btc = t.get("bank_transaction_code") or {}
    remit = " ".join(x for x in (t.get("remittance_information") or []) if x) or None
    booking = parse_date(t.get("booking_date") or t.get("value_date") or t.get("transaction_date"))
    if not booking:
        return None
    code = "-".join(x for x in (btc.get("code"), btc.get("sub_code")) if x) or None
    ref = t.get("entry_reference") or t.get("transaction_id")
    tx = {
        "account_id": account_id, "booking_date": booking, "value_date": parse_date(t.get("value_date")),
        "amount": round(amount, 2), "currency": (t.get("transaction_amount") or {}).get("currency") or "EUR",
        "status": "booked" if status == "BOOK" else "pending",
        "counterparty_name": cp.get("name"), "counterparty_iban": clean_iban(cp_acc.get("iban")),
        "description": " ".join(x for x in (btc.get("description"), t.get("note")) if x) or None,
        "remittance": " ".join(x for x in (t.get("reference_number"), remit) if x) or remit,
        "bank_tx_code": code, "mcc": t.get("merchant_category_code"), "source": "enablebanking", "raw_json": t,
    }
    if ref:
        tx["id"] = db.make_id("eb", account_id, ref, tx["status"])
    else:
        tx["id"] = db.make_id("eb", account_id, booking, tx["amount"], tx["counterparty_name"], tx["remittance"],
                              tx["status"], occurrence)
    return tx


def sync(conn, cfg: dict, force: bool = False, days: int = 730, client: Client | None = None,
         today: date | None = None) -> dict:
    client = client or Client.from_config(cfg)
    today = today or date.today()
    min_hours = float(cfg.get("min_sync_hours") or 6)
    summary = {"accounts": [], "errors": []}
    links = conn.execute("SELECT * FROM bank_links WHERE status='active'").fetchall()
    if not links:
        return {"accounts": [], "errors": ["No linked banks. Run `centen link --bank <name>` first."]}
    for link in links:
        if link["valid_until"] and link["valid_until"][:10] < today.isoformat():
            conn.execute("UPDATE bank_links SET status='expired' WHERE id=?", (link["id"],))
            summary["errors"].append(f"Consent for {link['aspsp_name']} expired on {link['valid_until'][:10]}: "
                                     f"link it again (update app → Banken, or `centen link --bank \"{link['aspsp_name']}\"`).")
            continue
        accounts = conn.execute("SELECT * FROM accounts WHERE link_id=?", (link["id"],)).fetchall()
        for acc in accounts:
            last = conn.execute("SELECT finished_at FROM sync_log WHERE account_id=? AND status='ok' "
                                "ORDER BY id DESC LIMIT 1", (acc["id"],)).fetchone()
            if last and not force:
                age_h = (datetime.now(timezone.utc) - datetime.fromisoformat(last[0])).total_seconds() / 3600
                if age_h < min_hours:
                    summary["accounts"].append({"account": acc["name"], "skipped": f"synced {age_h:.1f}h ago"})
                    continue
            started = datetime.now(timezone.utc).isoformat()
            try:
                bals = client.balances(acc["external_uid"])
                booked, avail = _pick(bals, _BOOKED), _pick(bals, _AVAILABLE)
                for kind, b in (("booked", booked), ("available", avail)):
                    if b:
                        db.set_balance(conn, acc["id"], parse_date(b.get("reference_date")) or today.isoformat(), kind,
                                       float(b["balance_amount"]["amount"]), b["balance_amount"].get("currency", "EUR"))
                newest = conn.execute("SELECT MAX(booking_date) FROM transactions WHERE account_id=? AND status='booked'",
                                      (acc["id"],)).fetchone()[0]
                date_from = (date.fromisoformat(newest) - timedelta(days=10)) if newest else today - timedelta(days=days)
                try:
                    raw = list(client.transactions(acc["external_uid"], date_from))
                except EnableBankingError as e:
                    if newest or e.status >= 500:
                        raise
                    # Many banks only serve ~90 days on later accesses; retry with a shorter window.
                    date_from = today - timedelta(days=89)
                    raw = list(client.transactions(acc["external_uid"], date_from))
                occ = Counter()
                mapped = []
                for t in raw:
                    k = (t.get("booking_date"), (t.get("transaction_amount") or {}).get("amount"), t.get("status"))
                    occ[k] += 1
                    m = map_transaction(t, acc["id"], occ[k])
                    if m:
                        mapped.append(m)
                ctx = Context.from_db(conn)
                mapped = [enrich(m, ctx) for m in mapped]
                # Pending items change id when they book: replace them wholesale on every sync.
                conn.execute("DELETE FROM transactions WHERE account_id=? AND status='pending' AND source='enablebanking'",
                             (acc["id"],))
                n_new = db.insert_transactions(conn, mapped)
                conn.execute("INSERT INTO sync_log(account_id, started_at, finished_at, status, message, n_new) "
                             "VALUES (?,?,?,?,?,?)", (acc["id"], started, datetime.now(timezone.utc).isoformat(), "ok",
                                                      f"from {date_from}", n_new))
                conn.commit()
                summary["accounts"].append({"account": acc["name"], "new_transactions": n_new,
                                            "fetched": len(mapped), "from": date_from.isoformat()})
            except EnableBankingError as e:
                conn.execute("INSERT INTO sync_log(account_id, started_at, finished_at, status, message, n_new) "
                             "VALUES (?,?,?,?,?,0)", (acc["id"], started, datetime.now(timezone.utc).isoformat(),
                                                      "error", str(e)))
                conn.commit()
                hint = " Consent may have expired or been revoked: run `centen link` again." if e.status in (401, 403) else ""
                summary["errors"].append(f"{acc['name']}: {e}.{hint}")
    conn.commit()
    if any(a.get("new_transactions") for a in summary["accounts"]):
        recategorize(conn)
    return summary


def unlink(conn, cfg: dict, link_id: str, client: Client | None = None) -> dict:
    row = conn.execute("SELECT * FROM bank_links WHERE id=?", (link_id,)).fetchone()
    if not row:
        raise ValueError(f"No link {link_id}")
    client = client or Client.from_config(cfg)
    try:
        client.delete_session(row["session_id"])
    except EnableBankingError as e:
        if e.status != 404:
            raise
    conn.execute("UPDATE bank_links SET status='revoked' WHERE id=?", (link_id,))
    conn.commit()
    return {"link_id": link_id, "status": "revoked"}
