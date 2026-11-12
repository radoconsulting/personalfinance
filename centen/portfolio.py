"""Portfolio management from broker exports: holdings with average cost, realised and unrealised results, dividends,
withholding tax, costs, stock-exchange tax (TOB), deposits and the money-weighted return (XIRR), per account and
together.

Cost method: average cost per instrument (GAK, gemiddelde aankoopkoers), costs of buying included. Selling books
the difference between the net proceeds and the average cost as realised result. Crypto received as a reward counts
at its value when received; crypto moved out (e.g. to a Ledger wallet) leaves at average cost, without a result.
These are management figures, not a tax calculation.
"""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import date
from pathlib import Path

from . import assets, brokers
from .categorize import INVESTING_ASSET_TYPES
from .money import normalize_text

EPS = 1e-9
_EQUIV = [{"lynx", "interactive brokers", "ibkr", "interactive"}, {"degiro", "flatex", "flatexdegiro"}, {"bitvavo"}]


def r2(x):
    return None if x is None else round(float(x), 2)


def _same_broker(acc, broker: str | None) -> bool:
    if not broker:
        return True
    hay = normalize_text(f"{acc['name']} {acc['bank'] or ''}")
    group = next((g for g in _EQUIV if normalize_text(broker) in g), {normalize_text(broker)})
    return any(re.search(rf"\b{re.escape(w)}\b", hay) for w in group)


# --- import -------------------------------------------------------------------------------------

def _expand(paths) -> list[Path]:
    out = []
    for p in ([paths] if isinstance(paths, str) else paths):
        q = Path(p).expanduser()
        if q.is_dir():
            out += sorted(x for x in q.iterdir() if x.suffix.lower() in (".csv", ".txt", ".pdf") and x.is_file())
        else:
            out.append(q)
    if not out:
        raise ValueError("No files found (CSV exports expected).")
    return out


def _content_key(d, tm, kind, isin, symbol, name, qty, price, amount, desc) -> tuple:
    """What makes two imported rows the same transaction, independent of the export they came from."""
    def r(x, n):
        return None if x is None else round(float(x), n)
    inst = isin or symbol or name
    if kind in ("buy", "sell"):
        return (d, (tm or "")[:5], kind, inst, r(qty, 6), r(price, 6))
    return (d, (tm or "")[:5], kind, inst, r(amount, 2), (desc or "").strip().lower()[:25])


INV_COLS = ("id", "account_id", "date", "time", "kind", "name", "isin", "symbol", "quantity", "price", "currency",
            "amount", "amount_eur", "fee_eur", "fx", "order_id", "description", "source", "file")


def import_files(conn, account: str, paths, force: bool = False, today: date | None = None) -> dict:
    """Import broker exports (files or a folder) into an investment account. Re-importing is safe."""
    acc = assets.find(conn, account)
    if acc["account_type"] not in INVESTING_ASSET_TYPES:
        raise ValueError(f"{acc['name']} is a {acc['account_type']} account; broker exports belong to an investment "
                         "or crypto account.")
    files, results = _expand(paths), []
    for f in files:
        try:
            res = brokers.parse_file(f)
        except ValueError as e:
            results.append({"file": f.name, "error": str(e)})
            continue
        broker = brokers.BROKER_OF.get(res["format"])
        if not force and not _same_broker(acc, broker):
            results.append({"file": f.name, "format": res["format"],
                            "error": f"This looks like a {broker} export, not {acc['name']}. Import it into the "
                                     f"{broker} account (or pass force=True)."})
            continue
        rows = []
        # An export of the same period can produce other ids than an earlier one (a newer DEGIRO export adds order
        # ids, an IBKR statement numbers its rows, descriptions get cut differently), so besides the id a row is
        # also skipped when the same content is already stored from another file often enough.
        stored = defaultdict(int)
        for e in conn.execute("SELECT date, time, kind, isin, symbol, name, quantity, price, amount, description "
                              "FROM inv_transactions WHERE account_id = ? AND source = ? AND file <> ?",
                              (acc["id"], res["format"], f.name)):
            stored[_content_key(*e)] += 1
        in_file = defaultdict(int)
        for t in res["transactions"]:
            t = dict(t, account_id=acc["id"], source=res["format"], file=f.name,
                     id=brokers.make_id(acc["id"], res["format"], t["ext"]))
            k = _content_key(t["date"], t["time"], t["kind"], t["isin"], t["symbol"], t["name"], t["quantity"],
                             t["price"], t["amount"], t["description"])
            in_file[k] += 1
            if in_file[k] <= stored[k]:
                continue
            rows.append([t.get(c) for c in INV_COLS])
        before = conn.total_changes
        conn.executemany(f"INSERT OR IGNORE INTO inv_transactions({','.join(INV_COLS)}) "
                         f"VALUES ({','.join('?' * len(INV_COLS))})", rows)
        new = conn.total_changes - before
        item = {"file": f.name, "format": res["format"], "rows": len(res["transactions"]), "new": new,
                "already_imported": len(res["transactions"]) - new, "from": res.get("from"), "to": res.get("to")}
        kinds = defaultdict(int)
        for t in res["transactions"]:
            kinds[t["kind"]] += 1
        if kinds:
            item["kinds"] = dict(kinds)
        if res.get("positions"):
            pos = res["positions"]
            sp = assets.set_positions(conn, acc["id"], pos["positions"], pos.get("as_of"), True, source=res["format"],
                                      total_value=pos.get("total_value"), today=today)
            item["positions"] = {"count": sp["positions"], "as_of": sp["as_of"], "total_value": sp["total_value"],
                                 "value_updated": sp["value_updated"]}
        if res.get("warnings"):
            item["warnings"] = res["warnings"][:10]
        results.append(item)
    conn.commit()
    out = {"account": acc["name"], "files": results}
    if any("error" not in r for r in results):
        p = account_portfolio(conn, acc, today)
        out["portfolio"] = {k: p[k] for k in ("totals", "warnings") if k in p}
        out["holdings"] = [{k: h.get(k) for k in ("name", "quantity", "avg_cost", "cost", "value", "unrealised")}
                           for h in p["holdings"]][:15]
    return out


# --- calculations -------------------------------------------------------------------------------

def _key(t) -> str:
    return (t["isin"] or (t["symbol"] or "").upper() or normalize_text(t["name"] or "")) or "?"


def _year():
    return {"deposits": 0.0, "withdrawals": 0.0, "bought": 0.0, "sold": 0.0, "realised": 0.0, "dividends": 0.0,
            "dividend_tax": 0.0, "interest": 0.0, "rewards": 0.0, "trade_costs": 0.0, "other_costs": 0.0,
            "tax": 0.0}


def _snapshot(conn, acc_id):
    d = conn.execute("SELECT MAX(as_of) FROM positions WHERE account_id=?", (acc_id,)).fetchone()[0]
    if not d:
        return None, []
    return d, [dict(r) for r in conn.execute("SELECT * FROM positions WHERE account_id=? AND as_of=?", (acc_id, d))]


def _dedupe_corporate(rows: list[dict]) -> list[dict]:
    """DEGIRO lists expiries (bonds, turbos) and some corporate actions in both exports, a few days apart. Keep one:
    the account-overview row when it carries the cash paid out (an expired bond's redemption), else the trade."""
    trades = [t for t in rows if t["source"] == "degiro_transactions" and t["isin"]]
    drop = set()
    for a in rows:
        if a["source"] != "degiro_account" or a["kind"] not in ("sell", "transfer_in", "buy") or not a["isin"]:
            continue
        da = date.fromisoformat(a["date"])
        for t in trades:
            if t["id"] in drop or t["isin"] != a["isin"] or abs((t["quantity"] or 0) - (a["quantity"] or 0)) > 1e-6 or \
                    abs((date.fromisoformat(t["date"]) - da).days) > 10 or \
                    (t["kind"] == "buy") != (a["kind"] in ("buy", "transfer_in")):
                continue
            if a["kind"] == "sell" and (a["amount_eur"] or 0) and not (t["amount_eur"] or 0):
                drop.add(t["id"])
            else:
                drop.add(a["id"])
            break
    return [r for r in rows if r["id"] not in drop]


def compute(conn, acc_id: str) -> dict:
    """Walk the broker transactions in date order and build holdings, results and yearly totals."""
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM inv_transactions WHERE account_id=? ORDER BY date, COALESCE(time,''), "
        "CASE kind WHEN 'deposit' THEN 0 WHEN 'buy' THEN 1 ELSE 2 END, rowid", (acc_id,))]
    rows = _dedupe_corporate(rows)
    pos: dict[str, dict] = {}
    years = defaultdict(_year)
    warnings = []
    names = {}
    for t in rows:
        if t["isin"] and t["name"]:
            names.setdefault(t["isin"], t["name"])
    for t in rows:
        y, k, q = years[t["date"][:4]], t["kind"], t["quantity"] or 0.0
        eur = t["amount_eur"]
        fee = t["fee_eur"] or 0.0
        if k in ("buy", "sell", "transfer_in", "transfer_out", "income") and q > EPS:
            key = _key(t)
            p = pos.setdefault(key, {"key": key, "name": names.get(t["isin"]) or t["name"], "isin": t["isin"],
                                     "symbol": t["symbol"], "currency": t["currency"], "quantity": 0.0, "cost": 0.0,
                                     "realised": 0.0, "dividends": 0.0, "dividend_tax": 0.0, "costs": 0.0,
                                     "first": t["date"], "last_price": None, "last_price_date": None, "fx": None,
                                     "moved_out": 0.0, "cost_unknown": False, "away_qty": 0.0,
                                     "away_cost": 0.0, "unknown_qty": 0.0})
            avg = p["cost"] / p["quantity"] if p["quantity"] > EPS else 0.0
            if t["price"] is not None and k in ("buy", "sell"):
                p["last_price"], p["last_price_date"], p["fx"] = t["price"], t["date"], t["fx"]
                p["currency"] = t["currency"] or p["currency"]
                p["price_kind"] = "last trade"
            elif t["price"] is not None and (p["last_price_date"] is None or t["date"] >= p["last_price_date"]):
                p["last_price"], p["last_price_date"], p["fx"] = t["price"], t["date"], None  # reward / transfer price
                p["price_kind"] = "price when received"
            if k == "buy":
                gross = -eur if eur is not None else (q * t["price"] if t["price"] and t["currency"] == "EUR" else None)
                if gross is None:
                    warnings.append(f"{t['date']}: buy of {p['name']} without EUR amount; cost left out")
                    p["cost_unknown"] = True
                    gross = 0.0
                p["quantity"] += q
                p["cost"] += gross + fee
                p["costs"] += fee
                y["bought"] += gross + fee
                y["trade_costs"] += fee
            elif k == "sell":
                if q > p["quantity"] + 1e-6:
                    warnings.append(f"{t['date']}: sold {q:g} {p['name']} but only {p['quantity']:g} bought in the "
                                    "imported history (import older exports for a correct cost)")
                    p["cost_unknown"] = True
                used = min(q, p["quantity"])
                cost_out = avg * used
                net = (eur if eur is not None else 0.0) - fee
                p["quantity"] = max(0.0, p["quantity"] - q)
                p["cost"] = max(0.0, p["cost"] - cost_out) if p["quantity"] > EPS else 0.0
                p["realised"] += net - cost_out
                p["costs"] += fee
                y["sold"] += net
                y["realised"] += net - cost_out
                y["trade_costs"] += fee
            elif k in ("transfer_in", "income"):
                basis = q * t["price"] if t["price"] else 0.0
                if k == "transfer_in" and t["price"] is None:
                    # units coming back from your own wallet (e.g. a Ledger) bring back the cost they left with
                    back = min(q, p["away_qty"])
                    if back > EPS:
                        share = p["away_cost"] * back / p["away_qty"]
                        basis, p["away_qty"], p["away_cost"] = share, p["away_qty"] - back, p["away_cost"] - share
                    if q - back > 1e-8:
                        p["unknown_qty"] += q - back  # deposited from elsewhere: purchase price unknown
                p["quantity"] += q
                p["cost"] += basis
                if k == "income":
                    y["rewards"] += basis
            else:  # transfer_out: leaves at average cost, no result; remembered in case it comes back
                used = min(q, p["quantity"])
                p["quantity"] = max(0.0, p["quantity"] - q)
                p["cost"] = max(0.0, p["cost"] - avg * used) if p["quantity"] > EPS else 0.0
                p["moved_out"] += q
                p["away_qty"] += used
                p["away_cost"] += avg * used
            if fee and k in ("transfer_out", "transfer_in"):
                y["other_costs"] += fee
            continue
        if eur is None:
            continue
        if k == "dividend":
            y["dividends"] += eur
            if t["isin"] or t["symbol"] or t["name"]:
                p = pos.get(_key(t))
                if p:
                    p["dividends"] += eur
        elif k == "dividend_tax":
            y["dividend_tax"] -= eur
            p = pos.get(_key(t))
            if p:
                p["dividend_tax"] -= eur
        elif k == "interest":
            y["interest"] += eur
        elif k == "income":
            y["rewards"] += eur
        elif k == "fee":
            y["other_costs"] -= eur
        elif k == "tax":
            y["tax"] -= eur
        elif k == "deposit":
            y["deposits"] += eur
        elif k == "withdrawal":
            y["withdrawals"] -= eur
    return {"positions": pos, "years": dict(sorted(years.items())), "warnings": warnings,
            "first": rows[0]["date"] if rows else None, "last": rows[-1]["date"] if rows else None,
            "count": len(rows)}


def _match(snapshot, h):
    for s in snapshot:
        if h["isin"] and s["isin"] and s["isin"].upper() == h["isin"].upper():
            return s
    for s in snapshot:
        if h["symbol"] and (s["symbol"] or "").upper() == h["symbol"].upper():
            return s
    n = normalize_text(h["name"] or "")
    return next((s for s in snapshot if n and normalize_text(s["name"]) == n), None)


def _live(h: dict, lp: dict | None, crypto: bool, qty: float | None) -> bool:
    """Apply a live price to a holding; True when used."""
    if not lp or lp.get("price_eur") is None or qty is None:
        return False
    h["price"], h["currency_price"] = lp["price"], lp["currency"]
    h["price_eur"] = round(lp["price_eur"], 6)
    h["value"] = r2(lp["price_eur"] * qty)
    h["price_source"] = f"live {(lp.get('quoted_at') or lp['fetched_at'])[:16].replace('T', ' ')} ({lp['symbol']})"
    if lp.get("prev_eur"):
        h["day_change_pct"] = round(lp["price_eur"] / lp["prev_eur"] - 1, 4)
        h["day_change"] = r2((lp["price_eur"] - lp["prev_eur"]) * qty)
    return True


def account_portfolio(conn, acc, today: date | None = None, include_closed: bool = False) -> dict:
    from .prices import latest as latest_prices
    today = today or date.today()
    c = compute(conn, acc["id"])
    snap_date, snap = _snapshot(conn, acc["id"])
    live = latest_prices(conn)
    crypto = acc["account_type"] == "crypto"
    used = set()
    holdings = []
    warnings = list(dict.fromkeys(c["warnings"]))[:15]
    for p in c["positions"].values():
        if p["quantity"] <= 1e-8 and not include_closed:
            continue
        s = _match(snap, p) if snap else None
        h = {"name": p["name"], "isin": p["isin"], "symbol": p["symbol"], "currency": p["currency"],
             "quantity": round(p["quantity"], 8), "avg_cost": r2(p["cost"] / p["quantity"]) if p["quantity"] > EPS
             else None, "cost": r2(p["cost"]), "realised": r2(p["realised"]),
             "dividends_net": r2(p["dividends"] - p["dividend_tax"]), "costs": r2(p["costs"]), "since": p["first"],
             "cost_complete": not p["cost_unknown"] and p["unknown_qty"] <= 0.05 * max(p["quantity"], EPS)}
        if p["unknown_qty"] > 1e-8:
            h["units_without_cost"] = round(min(p["unknown_qty"], p["quantity"]), 8)
        if p["away_qty"] > 1e-8:
            h["moved_out"] = round(p["away_qty"], 8)  # still outside the platform (e.g. on a hardware wallet)
            h["moved_out_cost"] = r2(p["away_cost"])
        newer_trades = s is not None and p["last_price_date"] and p["last_price_date"] > snap_date
        if s is not None and not newer_trades:
            used.add(s["id"])
            h.update({"price": s["price"], "value": r2(s["value"]), "price_source": f"export {snap_date}",
                      "asset_class": s["asset_class"]})
            if s["quantity"] is not None and abs(s["quantity"] - p["quantity"]) > 1e-6:
                h["quantity_in_export"] = s["quantity"]
                warnings.append(f"{p['name']}: {p['quantity']:g} from transactions, {s['quantity']:g} in the "
                                f"portfolio export of {snap_date} (import the full history)")
                h["cost_complete"] = False
        elif p["last_price"] is not None:
            px = p["last_price"]
            v = px * p["quantity"] if (p["currency"] or "EUR") == "EUR" else \
                (px * p["quantity"] / p["fx"] if p["fx"] else None)
            h.update({"price": px, "value": r2(v),
                      "price_source": f"{p.get('price_kind', 'last trade')} {p['last_price_date']}"})
        if s is not None:
            used.add(s["id"])
        lp = live.get((p["symbol"] or "").upper() if crypto else (p["isin"] or ""))
        if lp and (not snap_date or lp["date"] >= snap_date or newer_trades or s is None):
            _live(h, lp, crypto, h.get("quantity_in_export") or p["quantity"])
        if "asset_class" not in h or not h.get("asset_class"):
            h["asset_class"] = assets.guess_class(p["name"], p["isin"], acc["account_type"])
        if h.get("value") is not None and p["quantity"] > EPS and h["cost_complete"]:
            h["unrealised"] = r2(h["value"] - p["cost"])
            h["unrealised_pct"] = round((h["value"] - p["cost"]) / p["cost"], 4) if p["cost"] > 0 else None
        holdings.append(h)
    for s in snap:  # holdings in the export without transactions (history not imported, or cash)
        if s["id"] in used:
            continue
        lp = live.get(((s["symbol"] or "").upper() if crypto else (s["isin"] or "")))
        holdings.append({"name": s["name"], "isin": s["isin"], "symbol": s["symbol"], "currency": s["currency"],
                         "quantity": s["quantity"], "price": s["price"], "value": r2(s["value"]),
                         "price_source": f"export {snap_date}", "asset_class": s["asset_class"],
                         "cost": r2(s["cost"]) if s["cost"] is not None else None, "cost_complete": s["cost"] is not None,
                         "avg_cost": r2(s["cost"] / s["quantity"]) if s["cost"] and s["quantity"] else None,
                         **({"unrealised": r2(s["value"] - s["cost"])} if s["cost"] is not None and s["value"] is not None
                            and s["asset_class"] != "cash" else {})})
        if lp and lp["date"] >= snap_date and s["asset_class"] != "cash" and _live(holdings[-1], lp, crypto,
                                                                                    s["quantity"]):
            if s["cost"] is not None:
                holdings[-1]["unrealised"] = r2(holdings[-1]["value"] - s["cost"])
    holdings.sort(key=lambda h: -(h.get("value") or 0))
    total_value = sum(h.get("value") or 0 for h in holdings)
    for h in holdings:
        h["weight"] = round((h.get("value") or 0) / total_value, 4) if total_value else None
    yrs = {y: {k: r2(v) for k, v in d.items()} for y, d in c["years"].items()}
    tot = defaultdict(float)
    for d in c["years"].values():
        for k, v in d.items():
            tot[k] += v
    desc = assets.describe(conn, acc["id"], today)
    open_cost = sum(h["cost"] or 0 for h in holdings if h.get("cost_complete") and h.get("asset_class") != "cash")
    unreal = [h["unrealised"] for h in holdings if h.get("unrealised") is not None]
    totals = {
        "value": desc.get("value"), "value_as_of": desc.get("value_as_of"), "holdings_value": r2(total_value),
        "cost_of_holdings": r2(open_cost), "unrealised": r2(sum(unreal)) if unreal else None,
        "realised": r2(tot["realised"]), "dividends": r2(tot["dividends"]), "dividend_tax": r2(tot["dividend_tax"]),
        "interest": r2(tot["interest"]), "rewards": r2(tot["rewards"]), "trade_costs": r2(tot["trade_costs"]),
        "other_costs": r2(tot["other_costs"]), "tax": r2(tot["tax"]),
        "deposits": r2(tot["deposits"]), "withdrawals": r2(tot["withdrawals"]),
        "net_deposits": r2(tot["deposits"] - tot["withdrawals"]) if tot["deposits"] or tot["withdrawals"] else None,
        "contributions": desc.get("contributions"), "contributions_source": desc.get("contributions_source"),
        "day_change": r2(sum(h["day_change"] for h in holdings if h.get("day_change") is not None))
        if any(h.get("day_change") is not None for h in holdings) else None,
        "prices_as_of": max((h["price_source"][5:21] for h in holdings
                             if (h.get("price_source") or "").startswith("live")), default=None),
    }
    flows, src = assets.contribution_flows(conn, acc["id"])
    totals["xirr"] = None
    if src == "broker export" and desc.get("value") is not None and flows:
        cf = [(date.fromisoformat(d), -a) for d, a, _ in flows]
        cf.append((date.fromisoformat((desc.get("value_as_of") or today.isoformat())[:10]), desc["value"]))
        totals["xirr"] = xirr(cf)
    if snap_date:
        totals["export_date"] = snap_date
    return {"account": acc["name"], "provider": acc["bank"], "type": acc["account_type"],
            "history": {"transactions": c["count"], "from": c["first"], "to": c["last"]},
            "holdings": holdings, "totals": totals, "years": yrs, "warnings": warnings}


def xirr(flows: list[tuple[date, float]]) -> float | None:
    """Money-weighted yearly return. flows: (date, amount) from your point of view: money in = negative."""
    flows = [f for f in flows if abs(f[1]) > 0.005]
    if len(flows) < 2 or not any(a < 0 for _, a in flows) or not any(a > 0 for _, a in flows):
        return None
    t0 = min(d for d, _ in flows)
    span = (max(d for d, _ in flows) - t0).days
    if span < 30:
        return None

    def npv(r):
        return sum(a / (1 + r) ** ((d - t0).days / 365.0) for d, a in flows)
    lo, hi = -0.99, 10.0
    f_lo, f_hi = npv(lo), npv(hi)
    if f_lo * f_hi > 0:
        return None
    for _ in range(200):
        mid = (lo + hi) / 2
        f_mid = npv(mid)
        if abs(f_mid) < 1e-6:
            break
        if f_lo * f_mid < 0:
            hi = mid
        else:
            lo, f_lo = mid, f_mid
    return round(mid, 4)


# --- reports ------------------------------------------------------------------------------------

def _accounts(conn, account: str | None):
    if account:
        return [assets.find(conn, account)]
    return [r for r in conn.execute("SELECT * FROM accounts WHERE source='asset' ORDER BY name")
            if r["account_type"] in INVESTING_ASSET_TYPES and (
                conn.execute("SELECT 1 FROM inv_transactions WHERE account_id=? LIMIT 1", (r["id"],)).fetchone() or
                conn.execute("SELECT 1 FROM positions WHERE account_id=? LIMIT 1", (r["id"],)).fetchone())]


SUM_KEYS = ("value", "holdings_value", "day_change", "cost_of_holdings", "unrealised", "realised", "dividends", "dividend_tax",
            "interest", "rewards", "trade_costs", "other_costs", "tax", "deposits", "withdrawals")


def summary(conn, account: str | None = None, today: date | None = None, include_closed: bool = False) -> dict:
    today = today or date.today()
    accs = [account_portfolio(conn, a, today, include_closed) for a in _accounts(conn, account)]
    tot = {k: r2(sum(a["totals"].get(k) or 0 for a in accs)) for k in SUM_KEYS}
    years = defaultdict(lambda: defaultdict(float))
    by_class, by_ccy = defaultdict(float), defaultdict(float)
    holdings = []
    for a in accs:
        for y, d in a["years"].items():
            for k, v in d.items():
                years[y][k] += v or 0
        for h in a["holdings"]:
            holdings.append({"account": a["account"], **h})
            by_class[h.get("asset_class") or "other"] += h.get("value") or 0
            by_ccy[h.get("currency") or "EUR"] += h.get("value") or 0
    total = sum(h.get("value") or 0 for h in holdings)
    for h in holdings:
        h["weight_total"] = round((h.get("value") or 0) / total, 4) if total else None
    holdings.sort(key=lambda h: -(h.get("value") or 0))
    tot["prices_as_of"] = max((a["totals"]["prices_as_of"] for a in accs if a["totals"].get("prices_as_of")),
                              default=None)
    if not any(a["totals"].get("day_change") is not None for a in accs):
        tot["day_change"] = None
    return {
        "accounts": accs, "totals": tot,
        "years": {y: {k: r2(v) for k, v in d.items()} for y, d in sorted(years.items())},
        "holdings": holdings,
        "allocation": {"by_class": {k: r2(v) for k, v in sorted(by_class.items(), key=lambda kv: -kv[1])},
                       "by_currency": {k: r2(v) for k, v in sorted(by_ccy.items(), key=lambda kv: -kv[1])}},
        "method": "Average cost per instrument (purchase costs included); realised = net sale proceeds − average "
                  "cost. Values come from the latest portfolio export, else the last trade price. Not a tax "
                  "calculation.",
    }


def transactions(conn, account: str | None = None, instrument: str | None = None, kind: str | None = None,
                 date_from: str | None = None, date_to: str | None = None, limit: int = 100) -> dict:
    q, p = ("SELECT t.*, a.name AS account FROM inv_transactions t JOIN accounts a ON a.id=t.account_id WHERE 1=1",
            [])
    if account:
        q += " AND t.account_id=?"
        p.append(assets.find(conn, account)["id"])
    if instrument:
        q += " AND (t.isin=? OR upper(t.symbol)=? OR t.name LIKE ?)"
        p += [instrument.upper(), instrument.upper(), f"%{instrument}%"]
    if kind:
        q += " AND t.kind=?"
        p.append(kind)
    if date_from:
        q += " AND t.date >= ?"
        p.append(date_from)
    if date_to:
        q += " AND t.date <= ?"
        p.append(date_to)
    n = conn.execute(q.replace("SELECT t.*, a.name AS account", "SELECT COUNT(*), COALESCE(SUM(t.amount_eur),0)", 1),
                     p).fetchone()
    rows = conn.execute(q + " ORDER BY t.date DESC, t.time DESC LIMIT ?", p + [max(1, min(limit, 1000))]).fetchall()
    keep = ("account", "date", "kind", "name", "isin", "symbol", "quantity", "price", "currency", "amount", "amount_eur",
            "fee_eur", "description")
    return {"matches": n[0], "sum_eur": r2(n[1]), "transactions": [{k: r[k] for k in keep} for r in rows]}
