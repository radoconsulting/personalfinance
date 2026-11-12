"""Historical prices: daily closes around a date (the 31/12/2025 'fotomoment' for the capital gains tax) and month-end
closes for long histories (net worth history, benchmark comparison).

Same sources as `prices`: Yahoo Finance by ISIN (the stored symbol when there is one) and Bitvavo's public candles for
crypto. Only instrument codes are sent. Kept in their own table, `price_history`, so the live prices (`prices`) and
everything built on them stay exactly as they were.
"""
from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta, timezone

from . import prices as PR

SCHEMA = """
CREATE TABLE IF NOT EXISTS price_history (
    key         TEXT NOT NULL,           -- ISIN, coin symbol, or FX:<ccy> (units of ccy per EUR)
    date        TEXT NOT NULL,           -- trading day; for month bars the last day of the month
    close       REAL,                    -- in `currency`
    currency    TEXT,
    close_eur   REAL,
    interval    TEXT NOT NULL DEFAULT '1d',   -- 1d | 1mo
    source      TEXT,                    -- yahoo | bitvavo | manual | export
    symbol      TEXT,
    fetched_at  TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (key, date, interval)
);
"""

BITVAVO_CANDLES = "https://api.bitvavo.com/v2/{}/candles"
PHOTO_DATE = date(2025, 12, 31)


def ensure(conn) -> None:
    conn.executescript(SCHEMA)


def _ts(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp())


def _month_end(d: date) -> date:
    return date(d.year, d.month, calendar.monthrange(d.year, d.month)[1])


# --- fetching -----------------------------------------------------------------------------------

def _yahoo_series(http, symbol: str, start: date, end: date, interval: str) -> dict | None:
    try:
        r = http.get(PR.YAHOO_CHART.format(symbol), params={"period1": _ts(start), "period2": _ts(end + timedelta(days=1)),
                                                             "interval": interval, "events": "div,split"})
        if r.status_code != 200:
            return None
        res = (r.json().get("chart") or {}).get("result")
        if not res:
            return None
        meta, ts = res[0]["meta"], res[0].get("timestamp") or []
        closes = ((res[0].get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
        off = meta.get("gmtoffset") or 0
        out = {}
        for t, c in zip(ts, closes):
            if c is None:
                continue
            if interval == "1mo":
                # a month bar is stamped at midnight on the 1st, exchange time; the exchange's UTC offset can differ
                # from today's (summer time), so take the day 14 h later to stay safely inside the bar's month
                d = datetime.fromtimestamp(t + 14 * 3600, tz=timezone.utc).date()
                d = min(_month_end(d), end)
            else:
                d = datetime.fromtimestamp(t + off, tz=timezone.utc).date()
            if start <= d <= end or interval == "1mo":
                out[d] = float(c)
        return {"symbol": symbol, "currency": meta.get("currency") or "", "closes": out} if out else None
    except Exception:  # noqa: BLE001 - a missing history is reported, never fatal
        return None


def _bitvavo_series(http, coin: str, start: date, end: date) -> dict | None:
    """Daily closes (UTC days) from Bitvavo candles, in windows of at most 1440 days."""
    market = f"{coin.upper()}-EUR"
    out = {}
    s = start
    try:
        while s <= end:
            e = min(end, s + timedelta(days=1400))
            r = http.get(BITVAVO_CANDLES.format(market), params={"interval": "1d", "start": _ts(s) * 1000,
                                                                  "end": (_ts(e) + 86399) * 1000, "limit": 1440})
            if r.status_code != 200:
                break
            for row in r.json() or []:
                d = datetime.fromtimestamp(int(row[0]) / 1000, tz=timezone.utc).date()
                out[d] = float(row[4])
            s = e + timedelta(days=1)
    except Exception:  # noqa: BLE001
        pass
    return {"symbol": market, "currency": "EUR", "closes": out} if out else None


def _symbol_for(conn, http, key: str) -> list[str]:
    m = conn.execute("SELECT symbol FROM price_symbols WHERE key=? AND source='yahoo' AND symbol IS NOT NULL",
                     (key,)).fetchone()
    first = [m[0]] if m else []
    return list(dict.fromkeys(first + PR._candidates(http, key)))


def _store(conn, key, series, interval, source, fx=None):
    rows = []
    for d, c in series["closes"].items():
        cur = series["currency"]
        ce = c
        if cur in ("GBp", "GBX"):
            ce, cur_eur = c / 100.0, "GBP"
        else:
            cur_eur = cur
        if cur_eur and cur_eur != "EUR":
            rate = _nearest(fx or {}, d)
            ce = ce / rate if rate else None
        rows.append((key, d.isoformat(), c, series["currency"], ce, interval, source, series["symbol"]))
    conn.executemany("INSERT OR REPLACE INTO price_history(key, date, close, currency, close_eur, interval, source, "
                     "symbol) VALUES (?,?,?,?,?,?,?,?)", rows)
    return len(rows)


def _nearest(series: dict, d: date):
    best = None
    for k in sorted(series):
        if k <= d:
            best = series[k]
        else:
            break
    if best is None and series:
        best = series[min(series)]
    return best


def fetch(conn, instruments: dict[str, bool], start: date, end: date, interval: str = "1d", http=None) -> dict:
    """instruments: key -> is_crypto. Stores closes in price_history; returns what was found per key."""
    ensure(conn)
    http = PR._client(http)
    found, missing = {}, []
    fx_cache: dict[str, dict] = {}

    def fx_for(ccy):
        ccy = "GBP" if ccy in ("GBp", "GBX") else ccy
        if ccy == "EUR" or not ccy:
            return {}
        if ccy not in fx_cache:
            s = _yahoo_series(http, f"EUR{ccy}=X", start - timedelta(days=10), end, interval)
            fx_cache[ccy] = s["closes"] if s else {}
            if s:
                _store(conn, f"FX:{ccy}", {**s, "currency": ccy}, interval, "yahoo", fx={})
                conn.execute("UPDATE price_history SET close_eur=NULL WHERE key=?", (f"FX:{ccy}",))
        return fx_cache[ccy]

    for key, crypto in instruments.items():
        if not key:
            continue
        if crypto:
            if key.upper() in ("EUR",):
                continue
            coin = "USDC" if key.upper() in PR.STABLE else key
            s = _bitvavo_series(http, coin, start, end)
            if s and interval == "1mo":
                s["closes"] = {d: c for d, c in s["closes"].items() if d == _month_end(d) or d == max(s["closes"])}
            if s:
                found[key] = {"symbol": s["symbol"], "points": _store(conn, key, s, interval, "bitvavo")}
            else:
                missing.append(key)
            continue
        got = None
        for sym in _symbol_for(conn, http, key)[:6]:
            s = _yahoo_series(http, sym, start, end, interval)
            if not s:
                continue
            if got is None or (s["currency"] == "EUR" and got["currency"] != "EUR"):
                got = s
            if s["currency"] == "EUR":
                break
        if got:
            found[key] = {"symbol": got["symbol"], "currency": got["currency"],
                          "points": _store(conn, key, got, interval, "yahoo", fx_for(got["currency"]))}
        else:
            missing.append(key)
    conn.commit()
    return {"interval": interval, "from": start.isoformat(), "to": end.isoformat(), "found": found,
            "missing": missing}


# --- lookups ------------------------------------------------------------------------------------

def close_on(conn, key: str, d: date, interval: str | None = None, max_gap_days: int = 10) -> dict | None:
    """Last close in EUR on or before d (manual entries win), within max_gap_days."""
    ensure(conn)
    q = ("SELECT date, close_eur, close, currency, source, symbol FROM price_history WHERE key=? AND date<=? "
         "AND close_eur IS NOT NULL" + (" AND interval=?" if interval else "") +
         " ORDER BY (source='manual') DESC, date DESC LIMIT 1")
    args = (key, d.isoformat()) + ((interval,) if interval else ())
    r = conn.execute(q, args).fetchone()
    if not r or (d - date.fromisoformat(r[0])).days > max_gap_days and r[4] != "manual":
        return None
    return {"date": r[0], "price_eur": r[1], "price": r[2], "currency": r[3], "source": r[4], "symbol": r[5]}


def monthly_series(conn, key: str) -> dict[str, float]:
    """month 'YYYY-MM' -> close in EUR (from month bars, else the last daily close of that month)."""
    ensure(conn)
    month_bars, daily = {}, {}
    for d, c, iv in conn.execute("SELECT date, close_eur, interval FROM price_history WHERE key=? AND close_eur IS NOT "
                                 "NULL ORDER BY date", (key,)):
        (month_bars if iv == "1mo" else daily)[d[:7]] = c  # ordered by date: the last day of a month wins
    return {**daily, **month_bars}


def set_reference(conn, key: str, price_eur: float, d: date = PHOTO_DATE, note: str | None = None) -> dict:
    """Enter a price by hand (e.g. the value on your broker's 31/12/2025 statement); it wins over fetched prices."""
    ensure(conn)
    conn.execute("INSERT OR REPLACE INTO price_history(key, date, close, currency, close_eur, interval, source, symbol) "
                 "VALUES (?,?,?,?,?,?,?,?)", (key, d.isoformat(), price_eur, "EUR", price_eur, "1d", "manual", note))
    conn.commit()
    return {"instrument": key, "date": d.isoformat(), "price_eur": price_eur, "source": "manual"}


# --- what to fetch ------------------------------------------------------------------------------

def held_instruments(conn) -> dict[str, bool]:
    """Every instrument in broker transactions or positions: key -> is_crypto."""
    out = {}
    q = ("SELECT x.account_type, COALESCE(NULLIF(t.isin,''), UPPER(t.symbol)) FROM {} t JOIN accounts x ON "
         "x.id=t.account_id WHERE COALESCE(NULLIF(t.isin,''), t.symbol) IS NOT NULL")
    for table in ("inv_transactions", "positions"):
        for typ, key in conn.execute(q.format(table)):
            if key and key.upper() not in ("EUR", "CASH"):
                out[key] = out.get(key, False) or typ == "crypto"
    return out


def ensure_history(conn, http=None, today: date | None = None, force: bool = False) -> dict:
    """Fetch what's missing: daily closes around 31/12/2025 and month-end closes since the first broker transaction
    (plus the benchmark). Runs at most once a day unless force; only months not stored yet are asked for, and an
    instrument no source knows (delisted, renamed) is retried once a week."""
    import json
    from .db import get_meta, set_meta
    ensure(conn)
    today = today or date.today()
    if get_meta(conn, "history_format") != "2":  # month bars of format 1 could sit one month early (summer time)
        conn.execute("DELETE FROM price_history WHERE interval='1mo' AND source='yahoo'")
        set_meta(conn, "history_format", "2")
        conn.commit()
        force = True
    if not force and get_meta(conn, "history_checked") == today.isoformat():
        return {"skipped": "already checked today"}
    inst = held_instruments(conn)
    try:
        from .analysis import benchmark
        b = benchmark()["key"]
        inst.setdefault(b, False)
    except Exception:  # noqa: BLE001
        pass
    if not inst:
        return {"instruments": 0}
    unknown = json.loads(get_meta(conn, "history_missing") or "{}")
    recent = {k for k, d in unknown.items() if not force and (today - date.fromisoformat(d)).days < 7}
    out = {}
    photo_missing = {k: c for k, c in inst.items() if k not in recent and
                     close_on(conn, k, PHOTO_DATE, max_gap_days=12) is None}
    if today > PHOTO_DATE and photo_missing:
        out["photo"] = fetch(conn, photo_missing, PHOTO_DATE - timedelta(days=12), PHOTO_DATE, "1d", http)
    first = conn.execute("SELECT MIN(d) FROM (SELECT MIN(date) d FROM inv_transactions UNION ALL "
                         "SELECT MIN(as_of) FROM positions)").fetchone()[0]
    if first:
        start0 = date.fromisoformat(first[:7] + "-01")
        groups: dict[date, dict] = {}
        for k, c in inst.items():
            if k in recent:
                continue
            last = conn.execute("SELECT MAX(date) FROM price_history WHERE key=? AND interval='1mo'", (k,)).fetchone()[0]
            start = start0 if not last else max(start0, date.fromisoformat(last[:7] + "-01") - timedelta(days=31))
            groups.setdefault(date(start.year, start.month, 1), {})[k] = c
        found, missing = {}, []
        for start, keys in sorted(groups.items()):
            r = fetch(conn, keys, start, today, "1mo", http)
            found.update(r["found"])
            missing += r["missing"]
        out["monthly"] = {"interval": "1mo", "to": today.isoformat(), "found": found, "missing": missing}
    gone = set(out.get("monthly", {}).get("missing", [])) | set(out.get("photo", {}).get("missing", []))
    unknown = {k: d for k, d in unknown.items() if k in recent} | {k: today.isoformat() for k in gone}
    set_meta(conn, "history_missing", json.dumps(unknown))
    set_meta(conn, "history_checked", today.isoformat())
    conn.commit()
    return out
