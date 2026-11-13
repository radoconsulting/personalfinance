"""Net worth over time.

- A snapshot per day (`live`): the same figure the dashboard shows (cash + investments + property − loans), written
  whenever the dashboard is rebuilt (after a sync, an import, a price refresh). From now on this builds up by itself.
- A month-end reconstruction (`reconstructed`) of the past, from what centen already has:
  * bank accounts: today's balance minus the transactions after each month-end (before an account's history starts,
    its first known balance is carried back and flagged);
  * savings accounts entered by hand: the entered balance and the transfers seen before/after it;
  * broker accounts with a full export (DEGIRO, Bitvavo): the holdings at each month-end from the transactions,
    valued at that month's closing price, plus cash at the broker; crypto moved to your own wallet stays counted;
  * accounts with dated positions exports (LYNX): the latest export before each month-end at that month's prices;
  * other accounts (pension, insurance, property, loans): the values you entered (estimated in between) or the
    money paid in.
  Reconstructed months are estimates; the dashboard marks them as such.
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import date, timedelta

from . import history as H
from .categorize import INVESTING_ASSET_TYPES

SCHEMA = """
CREATE TABLE IF NOT EXISTS networth_snapshots (
    date            TEXT NOT NULL,
    source          TEXT NOT NULL,        -- live | reconstructed
    cash_personal   REAL,
    cash_business   REAL,
    investments     REAL,
    property        REAL,
    loans           REAL,
    total           REAL,
    parts_json      TEXT,                 -- per account (reconstructed) or the net_worth block (live)
    created_at      TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (date, source)
);
"""


def r2(x):
    return None if x is None else round(float(x), 2)


def snapshot(conn, today: date | None = None, position: dict | None = None) -> dict:
    """Store today's net worth (overwrites an earlier snapshot of the same day)."""
    from . import analytics as A
    today = today or date.today()
    if position is None:
        position = A.position(conn, date(today.year, 1, 1), today, today)
    nw, cash = position["net_worth"], position["cash"]
    row = {"date": today.isoformat(), "source": "live", "cash_personal": r2(cash.get("personal")),
           "cash_business": r2(cash.get("business")), "investments": r2(nw.get("investments")),
           "property": r2((nw.get("property") or 0) + (nw.get("possessions") or 0)), "loans": r2(abs(nw.get("loans") or 0)), "total": r2(nw.get("total"))}
    conn.execute("INSERT OR REPLACE INTO networth_snapshots(date, source, cash_personal, cash_business, investments, "
                 "property, loans, total, parts_json) VALUES (?,?,?,?,?,?,?,?,?)",
                 (*row.values(), json.dumps(nw, default=str)))
    conn.commit()
    return row


# --- reconstruction ---------------------------------------------------------------------------

def _month_ends(start: date, today: date) -> list[date]:
    from .analytics import add_months
    out, d = [], date(start.year, start.month, 1)
    while True:
        e = add_months(d, 1) - timedelta(days=1)
        if e >= today:
            break
        out.append(e)
        d = add_months(d, 1)
    return out


def _bank_balances(conn, ends: list[date]) -> tuple[dict, dict, list[str], dict]:
    """account_id -> {date: balance}; account_id -> scope; notes; account_id -> first day of its history."""
    from . import analytics as A
    from .manual import BALANCE_TYPE
    series, scopes, notes = {}, {}, []
    starts = {}
    for a in conn.execute("SELECT * FROM accounts WHERE source != 'asset'"):
        scopes[a["id"]] = a["scope"] or "personal"
        if a["source"] == "manual":
            m = conn.execute("SELECT amount, as_of FROM balances WHERE account_id=? AND balance_type=? ORDER BY as_of "
                             "DESC LIMIT 1", (a["id"], BALANCE_TYPE)).fetchone()
            if not m:
                continue
            moves = conn.execute("SELECT booking_date, -amount FROM transactions WHERE counterparty_iban=? AND "
                                 "status='booked' AND account_id != ?", (a["iban"], a["id"])).fetchall()
            s = {}
            for e in ends:
                ei = e.isoformat()
                if ei >= m["as_of"]:
                    s[e] = m["amount"] + sum(x for d, x in moves if m["as_of"] < d <= ei)
                else:
                    s[e] = m["amount"] - sum(x for d, x in moves if ei < d <= m["as_of"])
            series[a["id"]] = s
            continue
        b = A.latest_balance(conn, a["id"])
        v = b.get("booked") or b.get("available")
        if not v:
            continue
        bal, as_of = v
        txs = conn.execute("SELECT booking_date, amount FROM transactions WHERE account_id=? AND status='booked' "
                           "ORDER BY booking_date", (a["id"],)).fetchall()
        first = txs[0][0] if txs else as_of
        s = {}
        for e in ends:
            ei = e.isoformat()
            cut = max(ei, (date.fromisoformat(first) - timedelta(days=1)).isoformat())
            s[e] = bal - sum(x for d, x in txs if cut < d <= as_of)
        series[a["id"]] = s
        starts[a["id"]] = first
        if ends and ends[0].isoformat() < first:
            notes.append(f"{a['name']}: historie pas vanaf {first}; daarvoor is het eerste gekende saldo aangehouden")
    return series, scopes, notes, starts


def _walk_quantities(conn, acc_id, ends: list[date]):
    """Holdings, units moved to an own wallet, and broker cash at each month-end, from the broker transactions."""
    from .portfolio import _dedupe_corporate, _key
    rows = _dedupe_corporate([dict(r) for r in conn.execute(
        "SELECT * FROM inv_transactions WHERE account_id=? ORDER BY date, COALESCE(time,''), rowid", (acc_id,))])
    qty, away, cash = defaultdict(float), defaultdict(float), 0.0
    out, i = {}, 0
    for e in ends:
        ei = e.isoformat()
        while i < len(rows) and rows[i]["date"] <= ei:
            t = rows[i]
            i += 1
            k, q = _key(t), t["quantity"] or 0.0
            if t["kind"] in ("buy", "transfer_in", "income"):
                qty[k] += q
                if t["kind"] == "transfer_in" and t["price"] is None and away[k] > 0:
                    away[k] = max(0.0, away[k] - q)
            elif t["kind"] == "sell":
                qty[k] = max(0.0, qty[k] - q)
            elif t["kind"] == "transfer_out":
                qty[k] = max(0.0, qty[k] - q)
                away[k] += q
            if t["amount_eur"] is not None and t["kind"] not in ("transfer_in", "transfer_out", "income"):
                cash += t["amount_eur"] - (0.0 if t["source"] == "bitvavo" else (t["fee_eur"] or 0.0))
        out[e] = (dict(qty), dict(away), max(0.0, cash))
    return out


def reconstruct(conn, today: date | None = None, store: bool = True) -> dict:
    from . import assets as AS
    from . import loans as LN
    today = today or date.today()
    LN.ensure(conn)
    home_since = conn.execute("SELECT MIN(contract_date) FROM loan_terms").fetchone()[0]
    # Investments go back to their first transaction, value or position; cash (and so the total) only from the first
    # month with bank data, since before that it would be a guess.
    bank_first = conn.execute("SELECT MIN(booking_date) FROM transactions WHERE status='booked'").fetchone()[0]
    inv_first = conn.execute(
        "SELECT MIN(d) FROM (SELECT MIN(date) d FROM inv_transactions UNION ALL SELECT MIN(as_of) FROM positions "
        "UNION ALL SELECT MIN(b.as_of) FROM balances b JOIN accounts a ON a.id=b.account_id WHERE a.source='asset' "
        "UNION ALL SELECT MIN(booking_date) FROM transactions WHERE asset_id IS NOT NULL AND status='booked')"
    ).fetchone()[0]
    first = min(x for x in (bank_first, inv_first) if x) if (bank_first or inv_first) else None
    if not first:
        return {"months": 0}
    ends = _month_ends(date.fromisoformat(first), today)
    if not ends:
        return {"months": 0}
    bank_ends = [e for e in ends if bank_first and e.isoformat()[:7] >= bank_first[:7]]
    bank, scopes, notes, starts = _bank_balances(conn, bank_ends)
    names = {r[0]: f"{r[2] or ''} {r[1] or ''}".strip() for r in conn.execute("SELECT id, name, bank FROM accounts")}
    price_cache: dict[str, dict] = {}

    def px(key, e):
        if key not in price_cache:
            price_cache[key] = H.monthly_series(conn, key)
        s = price_cache[key]
        m = e.isoformat()[:7]
        if m in s:
            return s[m]
        before = [k for k in s if k <= m]
        return s[max(before)] if before else None

    inv = defaultdict(dict)       # account name -> {date: value}
    other = {"property": defaultdict(float), "loans": defaultdict(float)}
    missing_prices = set()
    walls = {}                    # crypto moved out: e -> key -> qty
    backcast = {}                 # accounts valued before their first entered value: name -> date of that value
    left_out = {}                 # accounts with no history at all before the bank data starts
    home_back = {}                # property counted at its first value before that value's date
    accs = list(conn.execute("SELECT * FROM accounts WHERE source='asset'"))
    for a in accs:
        typ = a["account_type"]
        fl, src = AS.contribution_flows(conn, a["id"])
        vals = AS._entries(conn, a["id"], AS.VALUATION)
        has_tx = conn.execute("SELECT 1 FROM inv_transactions WHERE account_id=? LIMIT 1", (a["id"],)).fetchone()
        snaps = [r[0] for r in conn.execute("SELECT DISTINCT as_of FROM positions WHERE account_id=? ORDER BY as_of",
                                            (a["id"],))]
        if typ in INVESTING_ASSET_TYPES and has_tx and src == "broker export":
            walk = _walk_quantities(conn, a["id"], ends)
            for e in ends:
                q, away, cash = walk[e]
                v = cash
                for k, n in q.items():
                    if n <= 1e-9:
                        continue
                    p = px(k, e)
                    if p is None:
                        missing_prices.add(k)
                        continue
                    v += n * p
                inv[a["name"]][e] = v
                if typ == "crypto":
                    walls[e] = {k: walls.get(e, {}).get(k, 0) + n for k, n in away.items() if n > 1e-9} | \
                               {k: n for k, n in walls.get(e, {}).items() if k not in away}
            continue
        if typ in INVESTING_ASSET_TYPES and snaps:
            for e in ends:
                ei = e.isoformat()
                before = [s for s in snaps if s <= ei]
                if not before:
                    if typ == "crypto" and walls.get(e):
                        held = {(r["symbol"] or "").upper(): r["quantity"] for r in conn.execute(
                            "SELECT symbol, quantity FROM positions WHERE account_id=? AND as_of=?", (a["id"], snaps[0]))}
                        v = 0.0
                        for k, n in walls[e].items():
                            n = min(n, held.get(k, 0.0))
                            p = px(k, e) if n > 0 else None
                            v += n * p if p else 0.0
                        inv[a["name"]][e] = v
                        if a["name"] not in [x.split(":")[0] for x in notes]:
                            notes.append(f"{a['name']}: vóór de eerste positie gewaardeerd met de crypto die van "
                                         "Bitvavo naar je eigen wallet ging")
                    else:
                        c = AS._contrib_at(AS._entries(conn, a["id"], AS.CONTRIBUTIONS), fl, e)
                        inv[a["name"]][e] = max(0.0, c or 0.0)
                    continue
                s = before[-1]
                v = 0.0
                for r in conn.execute("SELECT * FROM positions WHERE account_id=? AND as_of=?", (a["id"], s)):
                    k = r["isin"] or (r["symbol"] or "").upper()
                    p = px(k, e) if k and r["asset_class"] != "cash" and r["quantity"] else None
                    if p is not None:
                        v += r["quantity"] * p
                    elif r["value"] is not None:
                        v += r["value"]
                    elif k:
                        missing_prices.add(k)
                inv[a["name"]][e] = v
            continue
        # value entries / contributions only
        cs = [] if src == "broker export" else AS._entries(conn, a["id"], AS.CONTRIBUTIONS)
        plan = LN.terms(conn, a["id"]) if typ == "loan" else None
        for e in ends:
            if plan:  # a loan with a repayment plan: the amount owed at each month-end, from the schedule
                v = LN.balance_at(conn, a["id"], e, plan)
                if v:
                    other["loans"][e] += v
                continue
            if typ == "property" and vals and e < date.fromisoformat(vals[0][0][:10]) and home_since and \
                    e.isoformat() >= home_since:
                # the home before its first value: carried back to the start of its loans, at that value (estimate)
                other["property"][e] += abs(vals[0][1])
                home_back[a["name"]] = vals[0][0][:10]
                continue
            if typ in ("property", "loan", "asset"):  # possessions (a car, a watch) go with the home (r19)
                if not vals or e < date.fromisoformat(vals[0][0][:10]):
                    continue
                v = AS._value_at(a, vals, fl, e)
                if v is None:
                    continue
                other["loans" if typ == "loan" else "property"][e] += abs(v)
                continue
            first_val = date.fromisoformat(vals[0][0][:10]) if vals else None
            if first_val and e >= first_val:
                v = AS._value_at(a, vals, fl, e)
            elif first_val:
                # before the first value you entered: that value minus what was paid in since (an estimate), but only
                # as far back as the payments are known (the bank history); earlier the account is left out
                if not bank_first or e.isoformat()[:7] < bank_first[:7]:
                    # no payments known that far back: count it at the contributions you entered (as the investments
                    # view does), so the line doesn't jump when the bank history starts; marked as an estimate
                    left_out.setdefault(a["name"], bank_first)
                    c = AS._contrib_at(AS._entries(conn, a["id"], AS.CONTRIBUTIONS), fl, e)
                    if c and c > 0:
                        inv[a["name"]][e] = c
                    continue
                v = max(0.0, vals[0][1] - sum(f[1] for f in fl if e.isoformat() < f[0] <= vals[0][0][:10]))
                backcast.setdefault(a["name"], vals[0][0][:10])
            else:
                v = max(0.0, AS._contrib_at(cs, fl, e) or 0.0)
            inv[a["name"]][e] = v
    loose = [(r[0], -r[1]) for r in conn.execute(
        "SELECT booking_date, amount FROM transactions WHERE category='Savings & investments' AND asset_id IS NULL "
        "AND status='booked'")]
    for n, d in home_back.items():
        notes.append(f"{n}: vóór {d} aan dezelfde waarde geteld, vanaf het begin van de lening ({home_since})")
    if left_out:
        notes.append(", ".join(sorted(left_out)) + f": geen historiek vóór {bank_first[:7]}; daarvoor aan de ingegeven "
                     "inleg geteld (of niet, zonder inleg)")
    for n, d in backcast.items():
        notes.append(f"{n}: vóór de eerste ingegeven waarde ({d}) geschat als die waarde min wat je sindsdien inlegde")
    rows = []
    bank_set = set(bank_ends)
    for e in ends:
        iv = sum(s.get(e, 0.0) for s in inv.values()) + max(0.0, sum(x for d, x in loose if d <= e.isoformat()))
        pr, lo = other["property"].get(e, 0.0), other["loans"].get(e, 0.0)
        est = sorted([n for n, d in backcast.items() if e.isoformat() < d and (inv.get(n) or {}).get(e)] +
                     [n for n, d in home_back.items() if home_since and home_since <= e.isoformat() < d])
        parts = {"accounts": {n: r2(s.get(e)) for n, s in inv.items() if s.get(e)}, "estimated_accounts": est,
                 "not_counted": sorted(n for n in left_out if e not in (inv.get(n) or {})),
                 "at_contributions": sorted(n for n in left_out if e.isoformat()[:7] < (bank_first or "")[:7]
                                            and e in (inv.get(n) or {}))}
        if e in bank_set:
            cp = sum(s.get(e, 0.0) for k, s in bank.items() if scopes[k] == "personal")
            cb = sum(s.get(e, 0.0) for k, s in bank.items() if scopes[k] == "business")
            parts["carried_back"] = sorted(names[k] for k, d in starts.items() if d > e.isoformat())
            rows.append({"date": e.isoformat(), "cash_personal": r2(cp), "cash_business": r2(cb),
                         "investments": r2(iv), "property": r2(pr), "loans": r2(lo), "total": r2(cp + cb + iv + pr - lo),
                         "parts": parts})
        else:  # before the bank history: investments only, no cash and no total
            parts["carried_back"] = []
            parts["no_bank_data"] = True
            rows.append({"date": e.isoformat(), "cash_personal": None, "cash_business": None, "investments": r2(iv),
                         "property": r2(pr), "loans": r2(lo), "total": None, "parts": parts})
    if store:
        conn.execute("DELETE FROM networth_snapshots WHERE source='reconstructed'")
        conn.executemany("INSERT OR REPLACE INTO networth_snapshots(date, source, cash_personal, cash_business, "
                         "investments, property, loans, total, parts_json) VALUES (?,?,?,?,?,?,?,?,?)",
                         [(r["date"], "reconstructed", r["cash_personal"], r["cash_business"], r["investments"],
                           r["property"], r["loans"], r["total"], json.dumps(r["parts"])) for r in rows])
        from .db import set_meta
        set_meta(conn, "networth_reconstructed", json.dumps({"on": today.isoformat(), "notes": notes,
                                                             "missing_prices": sorted(missing_prices)}))
        conn.commit()
    return {"months": len(rows), "from": rows[0]["date"], "to": rows[-1]["date"], "notes": notes,
            "missing_prices": sorted(missing_prices), "last": rows[-1]}


def timeline(conn, months: int | None = None) -> dict:
    """Month-end points (reconstructed, then the last live snapshot of each month) plus the latest live value."""
    from .db import get_meta
    pts = {}
    for r in conn.execute("SELECT * FROM networth_snapshots ORDER BY date, source"):
        m = r["date"][:7]
        cur = pts.get(m)
        # a live snapshot beats a reconstruction of the same month; later days beat earlier ones
        if cur is None or (r["source"] == "live" and (cur["source"] != "live" or r["date"] >= cur["date"])) or \
                (r["source"] == cur["source"] and r["date"] >= cur["date"]):
            pts[m] = dict(r)
    ms = sorted(pts)
    if months:
        ms = ms[-months:]
    keep = ("date", "source", "cash_personal", "cash_business", "investments", "property", "loans", "total")
    points = []
    for m in ms:
        pt = {k: pts[m][k] for k in keep}
        if pts[m]["source"] == "reconstructed":
            try:
                pj = json.loads(pts[m]["parts_json"] or "{}")
            except ValueError:
                pj = {}
            pt["carried_back"] = pj.get("carried_back") or []
            pt["estimated_accounts"] = pj.get("estimated_accounts") or []
            pt["by_account"] = pj.get("accounts") or {}
            pt["not_counted"] = pj.get("not_counted") or []
            pt["at_contributions"] = pj.get("at_contributions") or []
            if pj.get("no_bank_data"):
                pt["no_bank_data"] = True
        points.append(pt)
    meta = get_meta(conn, "networth_reconstructed")
    info = json.loads(meta) if meta else {}
    out = {"points": points, "notes": info.get("notes", []), "missing_prices": info.get("missing_prices", []),
           "reconstructed_on": info.get("on")}
    if len(points) >= 2 and points[-1]["total"] is not None and points[-2]["total"] is not None:
        last, prev = points[-1], points[-2]
        out["change_last_month"] = r2(last["total"] - prev["total"])
        y = [p for p in points if p["date"][:7] == f"{int(last['date'][:4]) - 1}{last['date'][4:7]}"]
        if y and y[0]["total"] is not None:
            out["change_12_months"] = r2(last["total"] - y[0]["total"])
    tot = [p for p in points if p["total"] is not None]
    out["total_from"] = tot[0]["date"] if tot else None
    return out


def change_since(conn, days: int, today: date | None = None) -> dict | None:
    today = today or date.today()
    now = conn.execute("SELECT date, total FROM networth_snapshots WHERE source='live' ORDER BY date DESC LIMIT 1"
                       ).fetchone()
    then = conn.execute("SELECT date, total FROM networth_snapshots WHERE date <= ? AND total IS NOT NULL ORDER BY date DESC, "
                        "source='live' DESC LIMIT 1", ((today - timedelta(days=days)).isoformat(),)).fetchone()
    if not now or not then:
        return None
    return {"from": then[0], "to": now[0], "change": r2(now[1] - then[1]), "now": now[1]}
