"""Portfolio analysis on top of `portfolio`: allocation (asset class, region, currency, account) against targets you
set, what to buy or sell to get back on target, a dividend calendar for the coming twelve months, and a comparison
with a benchmark (what your own deposits would be worth had they all gone into one index fund).

Nothing here changes holdings or values; targets live in their own table (`allocation_targets`) and region overrides
in `instrument_meta`.
"""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import date, timedelta

from .money import normalize_text

SCHEMA = """
CREATE TABLE IF NOT EXISTS allocation_targets (
    dimension   TEXT NOT NULL,           -- class | region | currency | account
    bucket      TEXT NOT NULL,
    target      REAL NOT NULL,           -- share 0..1
    updated_at  TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (dimension, bucket)
);
CREATE TABLE IF NOT EXISTS instrument_meta (
    key         TEXT PRIMARY KEY,        -- ISIN or coin
    region      TEXT,
    asset_class TEXT,
    note        TEXT,
    updated_at  TEXT DEFAULT (datetime('now'))
);
"""

DIMENSIONS = ("class", "region", "currency", "account")
DEFAULT_BENCHMARK = {"key": "IE00BK5BQT80", "name": "Vanguard FTSE All-World (VWCE)"}
EPS = 1e-9

REGIONS = ("Wereld", "Wereld ex-VS", "VS", "Europa", "België", "Opkomende markten", "Japan", "Azië-Pacific",
           "Obligaties", "Crypto", "Cash", "Overig")
_REGION_RULES = [
    ("Opkomende markten", r"emerging|\bem\b|\bem imi\b|china|india|brazil|\bmsci em\b"),
    ("Wereld ex-VS", r"ex[- ]?us|ex usa|world ex|all-world ex"),
    ("Wereld", r"\bworld\b|\bwrld\b|all[- ]world|all[- ]country|cntry|\bacwi\b|\bglobal\b|ftse all|developed"),
    ("VS", r"s&p|s ?& ?p 500|\bsp500\b|\busa\b|\bus\b|nasdaq|dow jones|russell|msci usa|\bu\.s\."),
    ("Europa", r"europe|stoxx|\beuro\b|eurozone|\bemu\b|msci europe"),
    ("Japan", r"japan|nikkei|topix"),
    ("Azië-Pacific", r"pacific|asia|apac"),
]


def r2(x):
    return None if x is None else round(float(x), 2)


def ensure(conn) -> None:
    conn.executescript(SCHEMA)


def _cfg() -> dict:
    from . import config
    try:
        return config.load()
    except Exception:  # noqa: BLE001
        return {}


def benchmark() -> dict:
    b = _cfg().get("benchmark") or {}
    if isinstance(b, str):
        b = {"key": b, "name": b}
    return {**DEFAULT_BENCHMARK, **{k: v for k, v in b.items() if v}}


# --- classification ---------------------------------------------------------------------------

def region_of(name: str | None, isin: str | None, asset_class: str | None) -> str:
    if asset_class == "crypto":
        return "Crypto"
    if asset_class == "cash":
        return "Cash"
    if asset_class == "bond":
        return "Obligaties"
    n = normalize_text(name or "")
    if re.search(r"\bbond|obligati|treasury|covered|aggregate|\bgovt\b|corp(orate)? bond", n):
        return "Obligaties"
    if asset_class in ("etf", "fund", None, "other"):
        for region, rx in _REGION_RULES:
            if re.search(rx, n):
                return region
    if isin:
        cc = isin[:2]
        if cc == "US":
            return "VS"
        if cc == "BE":
            return "België"
        if cc in ("FR", "DE", "NL", "IT", "ES", "PT", "AT", "FI", "IE", "LU", "DK", "SE", "NO", "CH", "GB") \
                and asset_class == "equity":
            return "Europa"
        if cc == "JP":
            return "Japan"
    for region, rx in _REGION_RULES:
        if re.search(rx, n):
            return region
    return "Overig"


def _meta(conn) -> dict:
    ensure(conn)
    return {r[0]: {"region": r[1], "asset_class": r[2]} for r in
            conn.execute("SELECT key, region, asset_class FROM instrument_meta")}


def set_instrument(conn, instrument: str, region: str | None = None, asset_class: str | None = None) -> dict:
    """Fix the region (or class) of an instrument when the automatic guess is wrong."""
    ensure(conn)
    key = instrument.strip().upper()
    if region and region not in REGIONS:
        raise ValueError(f"Unknown region {region!r}; choose from {', '.join(REGIONS)}")
    conn.execute("INSERT INTO instrument_meta(key, region, asset_class) VALUES (?,?,?) ON CONFLICT(key) DO UPDATE SET "
                 "region=COALESCE(excluded.region, region), asset_class=COALESCE(excluded.asset_class, asset_class), "
                 "updated_at=datetime('now')", (key, region, asset_class))
    conn.commit()
    return {"instrument": key, "region": region, "asset_class": asset_class}


def holdings(conn, today: date | None = None) -> list[dict]:
    """All holdings (all broker / crypto accounts) with class, region, currency and EUR value."""
    from . import portfolio as PF
    meta = _meta(conn)
    pf = PF.summary(conn, today=today)
    out = []
    for h in pf["holdings"]:
        v = h.get("value")
        if v is None or abs(v) < 0.005:
            continue
        key = h.get("isin") or (h.get("symbol") or "").upper()
        m = meta.get(key or "", {})
        cls = m.get("asset_class") or h.get("asset_class") or "other"
        out.append({"account": h["account"], "name": h.get("name"), "key": key, "isin": h.get("isin"),
                    "class": cls, "region": m.get("region") or region_of(h.get("name"), h.get("isin"), cls),
                    "currency": (h.get("currency") or "EUR") if cls != "crypto" else "crypto",
                    "value": float(v), "quantity": h.get("quantity"), "price_eur": h.get("price_eur")})
    return out


# --- allocation and targets -------------------------------------------------------------------

def targets(conn) -> dict[str, dict[str, float]]:
    ensure(conn)
    out = defaultdict(dict)
    for d, b, t in conn.execute("SELECT dimension, bucket, target FROM allocation_targets"):
        out[d][b] = t
    return dict(out)


def set_targets(conn, dimension: str, targets_by_bucket: dict[str, float]) -> dict:
    """Replace the targets of one dimension. Shares may be given as 0.6 or 60; they must add up to 100 %."""
    ensure(conn)
    if dimension not in DIMENSIONS:
        raise ValueError(f"dimension must be one of {', '.join(DIMENSIONS)}")
    vals = {k.strip(): float(v) for k, v in targets_by_bucket.items() if k and k.strip()}
    if vals and max(vals.values()) > 1.0001:
        vals = {k: v / 100.0 for k, v in vals.items()}
    tot = sum(vals.values())
    if vals and abs(tot - 1.0) > 0.011:
        raise ValueError(f"Targets add up to {tot * 100:.1f} %, not 100 %.")
    conn.execute("DELETE FROM allocation_targets WHERE dimension=?", (dimension,))
    conn.executemany("INSERT INTO allocation_targets(dimension, bucket, target) VALUES (?,?,?)",
                     [(dimension, k, v) for k, v in vals.items()])
    conn.commit()
    return {"dimension": dimension, "targets": {k: round(v, 4) for k, v in vals.items()}}


def _plan(current: dict[str, float], target: dict[str, float], new_money: float) -> dict:
    """Buy-only plan for new money (fill the largest shortfalls first) and a full rebalance (buy and sell)."""
    total = sum(current.values())
    after = total + new_money
    buckets = sorted(set(current) | set(target))
    full = {b: r2(target.get(b, 0.0) * total - current.get(b, 0.0)) for b in buckets}
    short = {b: max(0.0, target.get(b, 0.0) * after - current.get(b, 0.0)) for b in buckets}
    need = sum(short.values())
    buy = {b: r2(new_money * s / need) if need > EPS else 0.0 for b, s in short.items()}
    return {"full_rebalance": full, "new_money": r2(new_money), "buy_with_new_money": buy}


def allocation(conn, today: date | None = None, new_money: float = 0.0, hs: list[dict] | None = None) -> dict:
    hs = holdings(conn, today) if hs is None else hs
    total = sum(h["value"] for h in hs)
    tg = targets(conn)
    dims = {}
    for d in DIMENSIONS:
        cur = defaultdict(float)
        for h in hs:
            cur[h[d]] += h["value"]
        t = tg.get(d, {})
        rows = []
        for b in sorted(set(cur) | set(t), key=lambda b: -cur.get(b, 0.0)):
            v = cur.get(b, 0.0)
            w = v / total if total else 0.0
            row = {"bucket": b, "value": r2(v), "weight": round(w, 4)}
            if t:
                row["target"] = round(t.get(b, 0.0), 4)
                row["deviation"] = round(w - t.get(b, 0.0), 4)
                row["deviation_eur"] = r2(v - t.get(b, 0.0) * total)
            rows.append(row)
        dims[d] = {"rows": rows, "has_targets": bool(t)}
        if t:
            dims[d]["plan"] = _plan(dict(cur), t, new_money)
            dims[d]["max_deviation"] = round(max(abs(r["deviation"]) for r in rows), 4)
    top = sorted(hs, key=lambda h: -h["value"])[:10]
    return {"total": r2(total), "dimensions": dims,
            "concentration": {"largest": [{"name": h["name"], "account": h["account"], "value": r2(h["value"]),
                                           "weight": round(h["value"] / total, 4) if total else None} for h in top[:5]],
                              "top5_weight": round(sum(h["value"] for h in top[:5]) / total, 4) if total else None},
            "notes": ["Regio = de focus van het fonds of het land van uitgifte (geen doorkijk in de onderliggende "
                      "aandelen): een wereldfonds telt als ‘Wereld’. Pas een regio aan met set_instrument_region."]}


# --- dividend calendar ------------------------------------------------------------------------

def _qty_at(rows: list[dict], key: str, d: str) -> float:
    q = 0.0
    for t in rows:
        if t["date"] > d:
            break
        if _key(t) != key:
            continue
        n = t["quantity"] or 0.0
        if t["kind"] in ("buy", "transfer_in", "income"):
            q += n
        elif t["kind"] in ("sell", "transfer_out"):
            q -= n
    return max(0.0, q)


def _key(t) -> str:
    from .portfolio import _key as pk
    return pk(t)


def dividend_calendar(conn, today: date | None = None, hs: list[dict] | None = None) -> dict:
    """Expected dividends for the next 12 months: per instrument still held, the payments of the last 12 months
    (per unit, at today's quantity) repeated in the same month. Gross, before withholding tax."""
    from .analytics import add_months
    from .portfolio import _dedupe_corporate
    today = today or date.today()
    hs = holdings(conn, today) if hs is None else hs
    held = defaultdict(float)
    for h in hs:
        if h["key"]:
            held[(h["account"], h["key"])] += h.get("quantity") or 0.0
    since = (today - timedelta(days=372)).isoformat()
    items, by_month = [], defaultdict(float)
    for acc in conn.execute("SELECT * FROM accounts WHERE source='asset' AND account_type IN ('investment','insurance',"
                            "'asset')"):
        rows = _dedupe_corporate([dict(r) for r in conn.execute(
            "SELECT * FROM inv_transactions WHERE account_id=? ORDER BY date, COALESCE(time,''), rowid", (acc["id"],))])
        taxes = defaultdict(float)
        for t in rows:
            if t["kind"] == "dividend_tax" and t["amount_eur"] is not None:
                taxes[(_key(t), t["date"])] += -t["amount_eur"]
        for t in rows:
            if t["kind"] != "dividend" or t["date"] < since or not t["amount_eur"] or t["amount_eur"] <= 0:
                continue
            k = _key(t)
            now_q = held.get((acc["name"], k), 0.0)
            if now_q <= EPS:
                continue
            then_q = _qty_at(rows, k, t["date"])
            per_unit = t["amount_eur"] / then_q if then_q > EPS else None
            expected = per_unit * now_q if per_unit is not None else t["amount_eur"]
            paid = date.fromisoformat(t["date"])
            nxt = add_months(paid, 12)
            while nxt <= today:
                nxt = add_months(nxt, 12)
            wh = taxes.get((k, t["date"]), 0.0)
            items.append({"account": acc["name"], "instrument": t["name"] or k, "key": k, "last_paid": t["date"],
                          "last_amount": r2(t["amount_eur"]), "expected_date": nxt.isoformat(),
                          "expected_gross": r2(expected),
                          "withholding_rate": round(wh / t["amount_eur"], 3) if wh else 0.0})
            by_month[nxt.isoformat()[:7]] += expected
    items.sort(key=lambda x: x["expected_date"])
    months = []
    d = date(today.year, today.month, 1)
    for _ in range(12):
        m = d.isoformat()[:7]
        months.append({"month": m, "expected_gross": r2(by_month.get(m, 0.0))})
        d = add_months(d, 1)
    return {"months": months, "items": items, "total_12m": r2(sum(by_month.values())),
            "note": "Schatting: dividenden van de voorbije 12 maanden per stuk, met je huidige aantal, in dezelfde "
                    "maand herhaald. Bruto, vóór bronheffing en roerende voorheffing. Accumulerende ETF's keren niets uit."}


# --- benchmark --------------------------------------------------------------------------------

def benchmark_compare(conn, today: date | None = None, bench: dict | None = None) -> dict:
    """Your deposits and withdrawals (from the broker exports) replayed into one benchmark fund at that month's
    closing price, compared with what the accounts are worth now. Needs month-end prices (history.ensure_history)."""
    from . import assets as AS
    from . import history as H
    from . import portfolio as PF
    from .prices import latest
    today = today or date.today()
    bench = bench or benchmark()
    series = H.monthly_series(conn, bench["key"])
    lp = latest(conn).get(bench["key"])
    if not series:
        return {"benchmark": bench, "available": False,
                "note": "Nog geen koershistoriek van de benchmark: wordt opgehaald bij de volgende prijsupdate."}
    now_px = (lp or {}).get("price_eur") or series[max(series)]
    accounts, flows_all = [], []
    for acc in PF._accounts(conn, None):
        if acc["account_type"] != "investment":
            continue  # crypto moves coins to and from own wallets: deposits alone don't tell what was invested
        fl, src = AS.contribution_flows(conn, acc["id"])
        if src != "broker export" or not fl:
            continue
        val = AS.describe(conn, acc["id"], today).get("value")
        if val is None:
            continue
        units, missing = 0.0, 0
        for d, amt, _ in fl:
            m = d[:7]
            px = series.get(m) or next((series[k] for k in sorted(series) if k >= m), None)
            if not px:
                missing += 1
                continue
            units += amt / px
        flows_all += [(date.fromisoformat(d), -a) for d, a, _ in fl]
        bval = max(0.0, units) * now_px
        accounts.append({"account": acc["name"], "since": fl[0][0], "net_deposits": r2(sum(a for _, a, _ in fl)),
                         "value": r2(val), "benchmark_value": r2(bval), "difference": r2(val - bval),
                         "flows_without_price": missing})
    if not accounts:
        return {"benchmark": bench, "available": False,
                "note": "Vergelijking vraagt stortingen uit een volledige brokerexport (DEGIRO, LYNX, Bitvavo)."}
    v, b = sum(a["value"] for a in accounts), sum(a["benchmark_value"] for a in accounts)
    xirr_own = PF.xirr(flows_all + [(today, v)]) if flows_all else None
    xirr_b = PF.xirr(flows_all + [(today, b)]) if flows_all else None
    # month-end value of the benchmark replay, to draw next to your own net worth line
    line, units, fl_sorted = [], 0.0, sorted((d, -a) for d, a in flows_all)
    i = 0
    for m in sorted(series):
        while i < len(fl_sorted) and fl_sorted[i][0].isoformat()[:7] <= m:
            units += fl_sorted[i][1] / series[m]
            i += 1
        if units:
            line.append({"month": m, "value": r2(max(0.0, units) * series[m])})
    return {"benchmark": bench, "available": True, "accounts": accounts, "value": r2(v), "benchmark_value": r2(b),
            "difference": r2(v - b), "xirr": xirr_own, "benchmark_xirr": xirr_b, "line": line,
            "note": "Elke storting en opname uit je brokerexports (beleggingsrekeningen, zonder crypto) in de benchmark "
                    "gestoken tegen de slotkoers van die maand; kosten en belastingen van de benchmark niet meegerekend."}


def report(conn, today: date | None = None, new_money: float = 0.0) -> dict:
    today = today or date.today()
    hs = holdings(conn, today)
    out = {"allocation": allocation(conn, today, new_money, hs), "dividends": dividend_calendar(conn, today, hs)}
    try:
        out["benchmark"] = benchmark_compare(conn, today)
    except Exception as e:  # noqa: BLE001 - a missing price history is never fatal
        out["benchmark"] = {"available": False, "note": f"Vergelijking niet beschikbaar: {e}"}
    return out
