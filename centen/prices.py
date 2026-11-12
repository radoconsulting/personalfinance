"""Current prices for your holdings.

- Stocks, ETFs and funds by ISIN from Yahoo Finance: the euro listing when there is one (Stuttgart lists most ETFs by
  ISIN), otherwise the main listing converted to EUR at the current exchange rate.
- Crypto from Bitvavo's public price feed (EUR markets, with the 24-hour change).

Only instrument codes are sent (ISINs, tickers, coin symbols); nothing about you or your amounts. Prices are kept per
day, so you also build up a price history. A symbol can be fixed by hand when the automatic choice is wrong.
"""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

from .categorize import INVESTING_ASSET_TYPES

YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{}"
YAHOO_SEARCH = "https://query2.finance.yahoo.com/v1/finance/search"
BITVAVO_24H = "https://api.bitvavo.com/v2/ticker/24h"
UA = {"User-Agent": "Mozilla/5.0 (centen personal finance)"}
EUR_SUFFIXES = (".SG", ".DE", ".AS", ".PA", ".BR", ".MI", ".F", ".MU", ".DU", ".BE", ".HM", ".VI", ".MC", ".LS",
                ".HE", ".IR")
STABLE = {"USDC", "USDT", "EURC", "DAI"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS price_symbols (
    key         TEXT PRIMARY KEY,        -- ISIN, or coin symbol for crypto
    source      TEXT NOT NULL,           -- yahoo | bitvavo
    symbol      TEXT,                    -- e.g. VWCE.DE, IE00BK5BQT80.SG, BTC-EUR
    currency    TEXT,
    manual      INTEGER DEFAULT 0,       -- set by hand: never replaced automatically
    checked_at  TEXT
);
CREATE TABLE IF NOT EXISTS prices (
    key         TEXT NOT NULL,
    date        TEXT NOT NULL,
    price       REAL,                    -- in `currency`
    currency    TEXT,
    price_eur   REAL,
    prev_eur    REAL,                    -- previous close (24 h ago for crypto), in EUR
    source      TEXT,
    symbol      TEXT,
    quoted_at   TEXT,                    -- time of the quote
    fetched_at  TEXT,
    PRIMARY KEY (key, date)
);
"""


def ensure(conn) -> None:
    conn.executescript(SCHEMA)


def _client(http=None):
    if http is not None:
        return http
    import httpx
    return httpx.Client(timeout=12.0, headers=UA, follow_redirects=True)


# --- what to price ----------------------------------------------------------------------------

def instruments(conn) -> dict[str, dict]:
    """Every instrument you hold (from broker exports and positions), keyed by ISIN or coin."""
    from . import portfolio
    out: dict[str, dict] = {}
    for acc in conn.execute("SELECT * FROM accounts WHERE source='asset'"):
        if acc["account_type"] not in INVESTING_ASSET_TYPES:
            continue
        crypto = acc["account_type"] == "crypto"
        c = portfolio.compute(conn, acc["id"])
        rows = [{"isin": p["isin"], "symbol": p["symbol"], "name": p["name"], "q": p["quantity"]}
                for p in c["positions"].values() if p["quantity"] > 1e-9]
        snap_date = conn.execute("SELECT MAX(as_of) FROM positions WHERE account_id=?", (acc["id"],)).fetchone()[0]
        if snap_date:
            rows += [{"isin": r["isin"], "symbol": r["symbol"], "name": r["name"], "q": r["quantity"] or 0}
                     for r in conn.execute("SELECT * FROM positions WHERE account_id=? AND as_of=? AND "
                                           "COALESCE(asset_class,'') != 'cash'", (acc["id"], snap_date))]
        for r in rows:
            if crypto or (r["symbol"] and not r["isin"] and acc["account_type"] == "crypto"):
                key = (r["symbol"] or r["name"] or "").upper()
                if key and key != "EUR":
                    out.setdefault(key, {"key": key, "kind": "crypto", "name": key})
            elif r["isin"]:
                out.setdefault(r["isin"], {"key": r["isin"], "kind": "security", "name": r["name"]})
    return out


# --- Yahoo ------------------------------------------------------------------------------------

def _chart(http, symbol: str) -> dict | None:
    try:
        r = http.get(YAHOO_CHART.format(symbol), params={"range": "5d", "interval": "1d"})
        if r.status_code != 200:
            return None
        res = (r.json().get("chart") or {}).get("result")
        if not res:
            return None
        m = res[0]["meta"]
        ts = res[0].get("timestamp") or []
        price = m.get("regularMarketPrice")
        if price is None:
            return None
        quoted = datetime.fromtimestamp(m.get("regularMarketTime") or 0)
        # previous close: the last daily close before the day of the quote. Pair each close with its own day before
        # dropping empty ones: today's bar often has no close yet, and dropping it first shifted every close one day.
        raw = ((res[0].get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
        off = m.get("gmtoffset") or 0
        day = lambda t: datetime.fromtimestamp(t + off, tz=timezone.utc).date()  # noqa: E731 - exchange's own date
        qday = day(m.get("regularMarketTime") or 0)
        before = [c for t, c in zip(ts, raw) if c is not None and day(t) < qday]
        prev = before[-1] if before else None
        return {"symbol": symbol, "price": float(price), "currency": m.get("currency") or "", "prev": prev,
                "quoted_at": quoted.isoformat(timespec="minutes")}
    except Exception:  # noqa: BLE001 - a missing price is reported, never fatal
        return None


def _candidates(http, isin: str, hints: list[str] | None = None) -> list[str]:
    # tickers your broker uses for this ISIN (e.g. LYNX: IUSA) on the euro exchanges come first
    hinted = [f"{h}{suf}" for h in (hints or []) for suf in (".AS", ".DE", ".MI", ".PA", ".BR")]
    syms = [f"{isin}.SG"]
    try:
        r = http.get(YAHOO_SEARCH, params={"q": isin, "quotesCount": 10, "newsCount": 0})
        quotes = r.json().get("quotes") or [] if r.status_code == 200 else []
    except Exception:  # noqa: BLE001
        quotes = []
    found = [q["symbol"] for q in quotes if q.get("symbol")]
    eur = [s for s in found if s.endswith(EUR_SUFFIXES)]
    other = [s for s in found if s not in eur]
    # a London listing often has the same ticker in Amsterdam or Xetra, in euro
    alt = [f"{s[:-2]}{suf}" for s in other if s.endswith(".L") for suf in (".AS", ".DE")]
    return list(dict.fromkeys(hinted + eur + syms + alt + other))


def _fx(http, ccy: str, cache: dict, prev: bool = False) -> float | None:
    """Units of `ccy` per EUR, now or at the previous close."""
    ccy = ccy.upper()
    if ccy == "EUR":
        return 1.0
    if ccy not in cache:
        q = _chart(http, f"EUR{ccy}=X")
        cache[ccy] = (q["price"], q.get("prev") or q["price"]) if q else (None, None)
        cache[ccy + ":prev"] = cache[ccy][1]
    v = cache[ccy]
    if not isinstance(v, tuple):  # an older cache holding only the rate
        return v
    return v[1] if prev else v[0]


def _to_eur(http, amount, currency: str, fx: dict, prev: bool = False):
    """In EUR; prev=True converts at the previous close's exchange rate, so the day change in EUR includes the
    currency move, like a euro listing of the same fund."""
    if amount is None:
        return None
    cur = currency or "EUR"
    if cur in ("GBp", "GBX"):  # London quotes in pence
        amount, cur = amount / 100.0, "GBP"
    rate = _fx(http, cur, fx, prev)
    return amount / rate if rate else None


def _security(http, key: str, m: dict | None, fx: dict, hints: list[str] | None = None) -> dict | None:
    """Network only (runs in a thread): price for an ISIN, using the stored symbol `m` if there is one. A stored
    non-euro listing that was picked automatically is looked at again, in case a euro listing turns up."""
    q = _chart(http, m["symbol"]) if m and m["symbol"] else None
    if not ((m and m["manual"]) or (q and q["currency"] == "EUR")):
        stored, best = q, None
        for s in _candidates(http, key, hints):
            c = _chart(http, s)
            if not c:
                continue
            fresh = datetime.fromisoformat(c["quoted_at"]) > datetime.now() - timedelta(days=7)
            if c["currency"] == "EUR" and fresh:
                best = c
                break
            if best is None and fresh:
                best = c
        q = best if best and (best["currency"] == "EUR" or stored is None) else stored or best
    if not q:
        return None
    q["price_eur"] = _to_eur(http, q["price"], q["currency"], fx)
    q["prev_eur"] = _to_eur(http, q["prev"], q["currency"], fx, prev=True)
    q["source"] = "yahoo"
    return q


# --- Bitvavo ----------------------------------------------------------------------------------

def _bitvavo(http) -> dict[str, dict]:
    try:
        r = http.get(BITVAVO_24H)
        data = r.json() if r.status_code == 200 else []
    except Exception:  # noqa: BLE001
        return {}
    out = {}
    for t in data if isinstance(data, list) else []:
        mk = t.get("market") or ""
        if not mk.endswith("-EUR") or t.get("last") in (None, ""):
            continue
        try:
            last, open_ = float(t["last"]), float(t["open"]) if t.get("open") not in (None, "") else None
        except (TypeError, ValueError):
            continue
        ts = t.get("closeTimestamp") or t.get("timestamp")
        out[mk[:-4]] = {"symbol": mk, "price": last, "currency": "EUR", "price_eur": last, "prev_eur": open_,
                        "source": "bitvavo",
                        "quoted_at": datetime.fromtimestamp(ts / 1000).isoformat(timespec="minutes") if ts else None}
    return out


# --- refresh ----------------------------------------------------------------------------------

def last_refresh(conn) -> str | None:
    ensure(conn)
    return conn.execute("SELECT MAX(fetched_at) FROM prices").fetchone()[0]


def refresh(conn, http=None, today: date | None = None, max_age_minutes: int = 0, update_values: bool = True) -> dict:
    """Fetch current prices for everything you hold. Skipped when the last refresh is younger than max_age_minutes."""
    ensure(conn)
    today = today or date.today()
    last = last_refresh(conn)
    if max_age_minutes and last and datetime.fromisoformat(last) > datetime.now() - timedelta(minutes=max_age_minutes):
        return {"skipped": f"prices are from {last[11:16]}, less than {max_age_minutes} minutes old"}
    items = instruments(conn)
    if not items:
        return {"updated": 0, "note": "No holdings yet: import broker exports or positions first."}
    own = http is None
    http = _client(http)
    fx: dict = {}
    got, missing = {}, []
    try:
        crypto = [k for k, v in items.items() if v["kind"] == "crypto"]
        if crypto:
            bv = _bitvavo(http)
            for k in crypto:
                if k in STABLE and k not in bv:
                    got[k] = {"symbol": k, "price": 1.0, "currency": "EUR", "price_eur": _to_eur(http, 1.0, "USD", fx)
                              if k != "EURC" else 1.0, "prev_eur": None, "source": "fixed", "quoted_at": None}
                elif k in bv:
                    got[k] = bv[k]
                else:
                    missing.append(k)
        secs = [k for k, v in items.items() if v["kind"] == "security"]
        maps = {r["key"]: dict(r) for r in conn.execute("SELECT * FROM price_symbols")}
        _fx(http, "USD", fx)  # warm the cache before the threads start
        _fx(http, "GBP", fx)
        hints: dict[str, list[str]] = {}
        for tbl in ("inv_transactions", "positions"):
            for isin, sym in conn.execute(f"SELECT DISTINCT isin, symbol FROM {tbl} WHERE isin IS NOT NULL AND "
                                          "symbol IS NOT NULL AND symbol != ''"):
                if sym.upper() not in hints.setdefault(isin, []):
                    hints[isin].append(sym.upper())
        with ThreadPoolExecutor(max_workers=6) as pool:
            for k, q in zip(secs, pool.map(lambda k: _security(http, k, maps.get(k), fx, hints.get(k)), secs)):
                (got.__setitem__(k, q) if q and q.get("price_eur") else missing.append(k))
        for k in secs:
            q = got.get(k)
            if q and not (maps.get(k) or {}).get("manual"):
                conn.execute("INSERT INTO price_symbols(key, source, symbol, currency, checked_at) VALUES "
                             "(?, 'yahoo', ?, ?, datetime('now')) ON CONFLICT(key) DO UPDATE SET "
                             "symbol=excluded.symbol, currency=excluded.currency, checked_at=excluded.checked_at",
                             (k, q["symbol"], q["currency"]))
    finally:
        if own:
            http.close()
    now = datetime.now().isoformat(timespec="seconds")
    conn.executemany(
        "INSERT INTO prices(key, date, price, currency, price_eur, prev_eur, source, symbol, quoted_at, fetched_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(key, date) DO UPDATE SET price=excluded.price, "
        "currency=excluded.currency, price_eur=excluded.price_eur, prev_eur=excluded.prev_eur, source=excluded.source, "
        "symbol=excluded.symbol, quoted_at=excluded.quoted_at, fetched_at=excluded.fetched_at",
        [(k, today.isoformat(), q["price"], q["currency"], q["price_eur"], q.get("prev_eur"), q["source"],
          q["symbol"], q.get("quoted_at"), now) for k, q in got.items()])
    conn.commit()
    out = {"updated": len(got), "missing": [{"key": k, "name": items[k]["name"]} for k in missing], "fetched_at": now,
           "symbols": {k: q["symbol"] for k, q in got.items()}}
    if update_values:
        out["account_values"] = update_account_values(conn, today)
    return out


def latest(conn) -> dict[str, dict]:
    """Newest price per instrument."""
    ensure(conn)
    rows = conn.execute("SELECT p.* FROM prices p JOIN (SELECT key, MAX(date) d FROM prices GROUP BY key) x "
                        "ON x.key=p.key AND x.d=p.date").fetchall()
    return {r["key"]: dict(r) for r in rows}


def update_account_values(conn, today: date | None = None) -> list[dict]:
    """With live prices for (nearly) everything in an account, its value for today becomes the live total."""
    from . import assets, portfolio
    today = today or date.today()
    out = []
    for acc in conn.execute("SELECT * FROM accounts WHERE source='asset'").fetchall():
        if acc["account_type"] not in INVESTING_ASSET_TYPES:
            continue
        p = portfolio.account_portfolio(conn, acc, today)
        hs = [h for h in p["holdings"] if h.get("value")]
        if not hs:
            continue
        live = sum(h["value"] for h in hs if (h.get("price_source") or "").startswith("live"))
        cash = sum(h["value"] for h in hs if h.get("asset_class") == "cash")
        total = sum(h["value"] for h in hs)
        if total and (live + cash) / total >= 0.95:
            assets._set(conn, acc["id"], assets.VALUATION, round(total, 2), today.isoformat())
            out.append({"account": acc["name"], "value": round(total, 2)})
    conn.commit()
    return out


def set_symbol(conn, instrument: str, symbol: str | None) -> dict:
    """Fix the price symbol for an ISIN or coin (e.g. 'IE0031442068' -> 'IUSA.AS'); None goes back to automatic."""
    ensure(conn)
    key = instrument.strip().upper()
    if not symbol:
        conn.execute("DELETE FROM price_symbols WHERE key=?", (key,))
        conn.commit()
        return {"instrument": key, "symbol": "automatic"}
    src = "bitvavo" if re.fullmatch(r"[A-Z0-9]+-EUR", symbol.upper()) else "yahoo"
    conn.execute("INSERT INTO price_symbols(key, source, symbol, manual, checked_at) VALUES (?,?,?,1,datetime('now')) "
                 "ON CONFLICT(key) DO UPDATE SET symbol=excluded.symbol, source=excluded.source, manual=1",
                 (key, src, symbol.strip()))
    conn.commit()
    return {"instrument": key, "symbol": symbol.strip(), "note": "Used from the next price refresh on."}
