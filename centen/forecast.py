"""Cash-flow forecast (r17): the expected balance of every bank account for the coming months, and of the personal,
business and total cash, so you see a dip before it happens (a yearly insurance premium, the VAT quarter, corporate
tax, a month with low income).

Built from three layers, per account:
1. Recurring payments and income (`analytics.recurring`) on their expected dates; fixed costs the user ignored or
   cancelled (`fixed.set_item`) are left out, manual fixed costs (`fixed.add_manual`) are added.
2. Seasonal items, taken from the same month last year: VAT, corporate tax, withholding tax and dividends for the
   company; the yearly tax bill or refund for the household. They don't come every month, but they do come.
3. Everything else as an average day-by-day flow: the mean per month over the last 6 complete months (groceries,
   restaurants, client revenue that varies, transfers between your own accounts, ...). Single payments of
   €5.000 or more in those months count as one-offs and are left out (they are listed), and money moved to
   investments is left out entirely: it's a choice, not a bill (its monthly average is shown).

It's an estimate: say so when it matters, and look at the dates of the low points rather than the exact amounts.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta

from . import analytics as A
from . import config, fixed

SEASONAL = {"VAT", "Corporate tax", "Withholding tax", "Dividends", "Taxes & social security", "Tax refund"}
REGULAR_LARGE = {"Revenue", "Salary", "Director remuneration", "Dividends", "Rental income", "VAT", "Corporate tax",
                 "Withholding tax", "Taxes & social security", "Tax refund", "Loans & mortgage", "Rent"}
ONE_OFF = 5000.0
BASE_MONTHS = 6
DEFAULT_BUFFER = {"personal": 1000.0, "business": 5000.0}


def r2(x):
    return None if x is None else round(float(x), 2)


def _buffer() -> dict:
    try:
        cfg = (config.load().get("forecast") or {}).get("buffer") or {}
    except Exception:  # noqa: BLE001
        cfg = {}
    return {**DEFAULT_BUFFER, **{k: float(v) for k, v in cfg.items() if k in DEFAULT_BUFFER}}


def _accounts(conn) -> list[dict]:
    out = []
    for a in conn.execute("SELECT id, name, bank, account_type, scope, source FROM accounts WHERE source != 'asset' "
                          "AND account_type IN ('current', 'savings', 'other') ORDER BY scope, account_type, name"):
        b = A.latest_balance(conn, a["id"])
        v = b.get("booked") or b.get("available")
        out.append({"id": a["id"], "name": a["name"], "bank": a["bank"], "type": a["account_type"],
                    "scope": a["scope"] or "personal", "balance": v[0] if v else None, "as_of": v[1] if v else None,
                    "manual": a["source"] == "manual"})
    return out


def _step(d: date, cadence: str) -> date:
    return A.add_months(d, 1) if cadence == "monthly" else d + timedelta(days=round(fixed.NOMINAL[cadence]))


def _recurring_events(conn, today: date, end: date, account_ids: set[str]) -> tuple[list[dict], set]:
    excluded = fixed.excluded_keys(conn)
    events, keys = [], set()
    for r in A.recurring(conn, today, include_income=True):
        if r["account_id"] not in account_ids:
            continue
        keys.add((r["account_id"], r["merchant_key"], r["direction"]))
        if not r["next_expected"] or (r["merchant_key"], r["scope"]) in excluded:
            continue
        if r["category"] == "Savings & investments":
            continue
        tol = next(c[4] for c in A.CADENCES if c[0] == r["cadence"])
        d = date.fromisoformat(r["next_expected"])
        while d < today - timedelta(days=tol):
            d = _step(d, r["cadence"])
        n = 0
        while d <= end and n < 120:
            when = max(d, today + timedelta(days=1))
            events.append({"date": when.isoformat(), "account_id": r["account_id"], "scope": r["scope"],
                           "name": r["merchant"], "category": r["category"],
                           "amount": r["typical_amount"] if r["direction"] == "in" else -r["typical_amount"],
                           "kind": "recurring", "cadence": r["cadence"], "late": d < today})
            d = _step(d, r["cadence"])
            n += 1
    return events, keys


def _month_range(today: date, months: int) -> tuple[date, date]:
    first_this = today.replace(day=1)
    return A.add_months(first_this, -months), first_this - timedelta(days=1)


def _baseline(conn, today: date, account_ids: set[str], recurring_keys: set) -> dict:
    """Mean monthly flow per (account, category) over the last complete months, without recurring, seasonal,
    investing and one-off payments."""
    start, stop = _month_range(today, BASE_MONTHS)
    rows = A.fetch(conn, start, stop, include_pending=False)
    first_seen = {r[0]: r[1] for r in conn.execute("SELECT account_id, MIN(booking_date) FROM transactions GROUP BY 1")}
    per = defaultdict(float)
    one_offs, investing = [], defaultdict(float)
    for r in rows:
        acc = r["account_id"]
        if acc not in account_ids:
            continue
        cat = r["category"] or "Other expenses"
        if (acc, r["merchant_key"], "out" if r["amount"] < 0 else "in") in recurring_keys and r["merchant_key"] not in (None, "unknown"):
            continue
        if cat == "Savings & investments":
            investing[acc] += r["amount"]
            continue
        if cat in SEASONAL:
            continue
        if abs(r["amount"]) >= ONE_OFF and cat not in REGULAR_LARGE:
            one_offs.append({"date": r["booking_date"], "account_id": acc, "name": r["merchant"], "category": cat,
                             "amount": r2(r["amount"])})
            continue
        per[(acc, cat)] += r["amount"]
    months_of = {}
    for acc in account_ids:
        fs = first_seen.get(acc)
        if not fs:
            months_of[acc] = BASE_MONTHS
            continue
        fsd = date.fromisoformat(fs)
        full = BASE_MONTHS if fsd <= start else max(1, (stop.year - fsd.year) * 12 + stop.month - fsd.month)
        months_of[acc] = min(BASE_MONTHS, full)
    mean = {k: v / months_of.get(k[0], BASE_MONTHS) for k, v in per.items()}
    inv = {k: v / months_of.get(k, BASE_MONTHS) for k, v in investing.items()}
    return {"mean": mean, "one_offs": one_offs, "investing": inv, "from": start.isoformat(), "to": stop.isoformat()}


def _seasonal_events(conn, today: date, end: date, account_ids: set[str]) -> list[dict]:
    rows = A.fetch(conn, A.add_months(today, -12) + timedelta(days=1), A.add_months(end, -12), include_pending=False)
    out = []
    for r in rows:
        if r["account_id"] not in account_ids or (r["category"] or "") not in SEASONAL:
            continue
        d = date.fromisoformat(r["booking_date"])
        nd = A.add_months(d, 12)
        if nd <= today or nd > end:
            continue
        out.append({"date": nd.isoformat(), "account_id": r["account_id"], "name": r["merchant"],
                    "category": r["category"], "amount": r2(r["amount"]), "kind": "seasonal",
                    "based_on": r["booking_date"]})
    return out


def forecast(conn, today: date | None = None, months: int = 6) -> dict:
    """Day-by-day expected balances for `months` months, per account and per perspective."""
    today = today or date.today()
    months = max(1, min(int(months), 18))
    end = A.add_months(today, months)
    accs = [a for a in _accounts(conn) if a["balance"] is not None]
    ids = {a["id"] for a in accs}
    scope_of = {a["id"]: a["scope"] for a in accs}
    name_of = {a["id"]: a["name"] for a in accs}
    rec_events, rec_keys = _recurring_events(conn, today, end, ids)
    base = _baseline(conn, today, ids, rec_keys)
    seas = _seasonal_events(conn, today, end, ids)
    manual = [dict(e, account_id=None, kind="manual") for e in fixed.expected_payments(conn, today + timedelta(days=1), end, today)]
    for e in rec_events + seas:
        e["scope"] = scope_of[e["account_id"]]
    events = rec_events + seas + manual

    # Daily per account (manual items go to a per-scope pseudo account).
    days = [(today + timedelta(days=i)) for i in range((end - today).days + 1)]
    per_day_var = defaultdict(float)  # account -> average flow per day
    for (acc, _cat), m in base["mean"].items():
        per_day_var[acc] += m * 12 / 365.25
    by_day = defaultdict(lambda: defaultdict(float))
    for e in events:
        by_day[e["account_id"] or f"manual:{e['scope']}"][e["date"]] += e["amount"]
    bal = {a["id"]: a["balance"] for a in accs}
    pseudo = {f"manual:{s}": 0.0 for s in ("personal", "business")}
    scope_of.update({"manual:personal": "personal", "manual:business": "business"})
    all_ids = list(bal) + list(pseudo)
    bal.update(pseudo)
    series = {p: [] for p in ("personal", "business", "total")}
    acc_min = {i: (bal[i], today.isoformat()) for i in all_ids}
    acc_series = {i: [] for i in bal if not i.startswith("manual:")}
    monthly = defaultdict(lambda: {"in": 0.0, "out": 0.0})
    for d in days:
        ds = d.isoformat()
        if d > today:
            for i in all_ids:
                flow = per_day_var.get(i, 0.0)
                ev = by_day[i].get(ds, 0.0)
                bal[i] += flow + ev
                sc = scope_of[i]
                for amt in (flow, ev):
                    key = (sc, ds[:7])
                    monthly[key]["in" if amt > 0 else "out"] += amt
                if bal[i] < acc_min[i][0]:
                    acc_min[i] = (bal[i], ds)
        tot = defaultdict(float)
        for i in all_ids:
            tot[scope_of[i]] += bal[i]
        if d == today or d.weekday() == 0 or d == end or d.day == 1:
            for p in ("personal", "business"):
                series[p].append({"date": ds, "balance": r2(tot[p])})
            series["total"].append({"date": ds, "balance": r2(tot["personal"] + tot["business"])})
            for i in acc_series:
                acc_series[i].append(r2(bal[i]))
        if d == today:
            start_tot = dict(tot)
        last_tot = dict(tot)

    # Exact lows per perspective (daily, not only the plotted points).
    lows = {}
    run = {p: start_tot.get(p, 0.0) for p in ("personal", "business")}
    lows = {p: (run[p], today.isoformat()) for p in run}
    lows["total"] = (run["personal"] + run["business"], today.isoformat())
    for d in days[1:]:
        ds = d.isoformat()
        for i in all_ids:
            run[scope_of[i]] += per_day_var.get(i, 0.0) + by_day[i].get(ds, 0.0)
        for p in ("personal", "business"):
            if run[p] < lows[p][0]:
                lows[p] = (run[p], ds)
        t = run["personal"] + run["business"]
        if t < lows["total"][0]:
            lows["total"] = (t, ds)

    buf = _buffer()
    warnings = []
    acc_out = []
    for a in accs:
        mn, md = acc_min[a["id"]]
        row = {"name": a["name"], "bank": a["bank"], "scope": a["scope"], "type": a["type"],
               "start": r2(a["balance"]), "as_of": a["as_of"], "end": r2(bal[a["id"]]), "min": r2(mn), "min_date": md,
               "variable_monthly": r2(per_day_var.get(a["id"], 0.0) * 365.25 / 12),
               "investing_monthly_avg": r2(base["investing"].get(a["id"])), "estimated_balance": a["manual"]}
        acc_out.append(row)
        if a["type"] == "current" and mn < 0:
            warnings.append({"severity": "critical", "scope": a["scope"], "account": a["name"], "date": md,
                             "amount": r2(mn), "text": f"{a['name']} kan rond {md} onder nul gaan ({r2(mn)})."})
    have = {a["scope"] for a in accs}
    for p in ("personal", "business"):
        if p not in have:
            continue
        mn, md = lows[p]
        if mn < buf[p]:
            warnings.append({"severity": "warning", "scope": p, "date": md, "amount": r2(mn),
                             "text": f"{'Privé' if p == 'personal' else 'BV'}: cash zakt rond {md} tot {r2(mn)}, "
                                     f"onder de buffer van {buf[p]:.0f}."})

    month_rows = []
    for p in ("personal", "business", "total"):
        for m in sorted({k[1] for k in monthly}):
            if p == "total":
                i = sum(monthly[(s, m)]["in"] for s in ("personal", "business"))
                o = sum(monthly[(s, m)]["out"] for s in ("personal", "business"))
            else:
                i, o = monthly[(p, m)]["in"], monthly[(p, m)]["out"]
            month_rows.append({"perspective": p, "month": m, "in": r2(i), "out": r2(-o), "net": r2(i + o),
                               "partial": m in (today.isoformat()[:7], end.isoformat()[:7])})

    baseline_rows = defaultdict(float)
    for (acc, cat), m in base["mean"].items():
        baseline_rows[(scope_of[acc], cat)] += m
    soon = today + timedelta(days=60)
    ev_public = sorted([{"date": e["date"], "account": name_of.get(e["account_id"], "—") if e["account_id"] else "—",
                         "scope": e["scope"], "name": e["name"], "category": e.get("category"),
                         "amount": r2(e["amount"]), "kind": e["kind"], "cadence": e.get("cadence"),
                         "based_on": e.get("based_on")}
                        for e in events if date.fromisoformat(e["date"]) <= soon], key=lambda x: (x["date"], x["amount"]))
    persp = {}
    for p in ("personal", "business", "total"):
        s0 = (start_tot.get("personal", 0) + start_tot.get("business", 0)) if p == "total" else start_tot.get(p)
        e0 = (last_tot.get("personal", 0) + last_tot.get("business", 0)) if p == "total" else last_tot.get(p)
        if p != "total" and p not in have:
            persp[p] = None
            continue
        persp[p] = {"start": r2(s0), "end": r2(e0), "change": r2((e0 or 0) - (s0 or 0)), "min": r2(lows[p][0]),
                    "min_date": lows[p][1], "buffer": buf.get(p), "series": series[p]}
    return {
        "from": today.isoformat(), "to": end.isoformat(), "months": months, "perspectives": persp,
        "accounts": acc_out, "account_series": {name_of[i]: v for i, v in acc_series.items()},
        "series_dates": [x["date"] for x in series["total"]],
        "monthly": month_rows, "events_next_60_days": ev_public,
        "variable": sorted([{"scope": s, "category": c, "monthly": r2(m)} for (s, c), m in baseline_rows.items()
                            if abs(m) >= 1], key=lambda x: x["monthly"]),
        "one_offs_left_out": sorted(base["one_offs"], key=lambda x: x["amount"])[:20],
        "investing_monthly_avg": {s: r2(sum(v for k, v in base["investing"].items() if scope_of.get(k) == s))
                                  for s in ("personal", "business")},
        "warnings": warnings,
        "assumptions": [
            "Vaste betalingen en inkomsten op hun verwachte datum (gebruikelijk bedrag); wat je negeerde of opzegde "
            "telt niet, wat je zelf toevoegde wel.",
            "Btw, vennootschapsbelasting, roerende voorheffing, dividend en de belastingaanslag of -teruggave zoals "
            "in dezelfde maand vorig jaar.",
            f"De rest als gemiddelde van {base['from'][:7]} t.e.m. {base['to'][:7]}, gespreid over de dagen; losse "
            f"betalingen vanaf € {ONE_OFF:.0f} tellen als eenmalig en vallen weg.",
            "Geld naar beleggingen telt niet mee (dat kies je zelf): zie het gemiddelde per maand.",
            "Spaarrekeningen die de bank niet deelt, tellen aan hun geschatte saldo, zonder interest.",
        ],
    }


def summary(conn, today: date | None = None, months: int = 3) -> dict:
    """Small version for the digest and notifications."""
    f = forecast(conn, today, months)
    out = {"to": f["to"], "warnings": f["warnings"]}
    for p in ("personal", "business"):
        v = f["perspectives"].get(p)
        out[p] = {k: v[k] for k in ("start", "end", "min", "min_date")} if v else None
    return out
