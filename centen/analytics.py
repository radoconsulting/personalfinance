"""The analyses behind the Personal Finance tools. Pure functions over the SQLite store."""
from __future__ import annotations

import calendar
import re
import sqlite3
import statistics
from collections import defaultdict
from datetime import date, timedelta

from .categorize import GROUP, KIND
from .money import mask_ibans_in_text, normalize_text

PERSPECTIVES = ("personal", "business", "total")

# Categories that are habitual spending rather than bills; they only count as recurring when paid by
# direct debit or standing order.
HABITUAL = {"Groceries", "Restaurants & takeaway", "Fuel", "EV charging", "Cash withdrawal", "Shopping",
            "Clothing & shoes", "Electronics", "Drugstore & personal care", "Online payments", "Parking & tolls",
            "Public transport & taxi", "Leisure & sport", "Healthcare", "Pets", "Home & DIY", "Travel & holidays"}

CADENCES = [  # name, nominal days, min, max, tolerance
    ("weekly", 7, 5, 9, 2), ("every 2 weeks", 14, 12, 16, 3), ("monthly", 30.44, 26, 35, 6),
    ("every 2 months", 61, 55, 67, 8), ("quarterly", 91, 82, 100, 12), ("every 6 months", 182, 165, 200, 20),
    ("yearly", 365, 340, 390, 25),
]


def _d(s: str) -> date:
    return date.fromisoformat(s[:10])


def add_months(d: date, n: int, day: int | None = None) -> date:
    m = d.month - 1 + n
    y, m = d.year + m // 12, m % 12 + 1
    last = calendar.monthrange(y, m)[1]
    return date(y, m, min(day or d.day, last))


def r2(x) -> float:
    return round(float(x or 0), 2)


def tx_public(r: sqlite3.Row | dict, account_names: dict | None = None) -> dict:
    """The shape every tool uses for a transaction. Full IBANs and raw bank payloads never appear here."""
    r = dict(r)
    out = {
        "id": r["id"], "date": r["booking_date"], "amount": r2(r["amount"]), "currency": r.get("currency") or "EUR",
        "status": r.get("status"), "merchant": r.get("merchant"), "counterparty": r.get("counterparty_name"),
        "counterparty_account": r.get("counterparty_masked"), "category": r.get("category"), "kind": r.get("kind"),
        "payment_type": r.get("payment_type"),
        "description": mask_ibans_in_text((r.get("description") or "")[:160]) or None,
        "message": mask_ibans_in_text((r.get("remittance") or "")[:160]) or None,
    }
    if r.get("structured_ref"):
        out["structured_ref"] = r["structured_ref"]
    if account_names is not None:
        out["account"] = account_names.get(r["account_id"], r["account_id"])
    return out


def account_names(conn) -> dict:
    return {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM accounts")}


def _check_perspective(perspective):
    if perspective not in (None, "all") + PERSPECTIVES:
        raise ValueError("perspective must be personal, business or total")


def fetch(conn, date_from: date | None = None, date_to: date | None = None, kinds=None, accounts=None,
          include_pending: bool = True, perspective: str | None = None) -> list[dict]:
    """perspective: personal / business = only accounts of that scope; total = all accounts, without money moving
    between your company and you (counted once); None = everything."""
    _check_perspective(perspective)
    q, p = "SELECT * FROM transactions WHERE 1=1", []
    if perspective in ("personal", "business"):
        q += " AND account_id IN (SELECT id FROM accounts WHERE COALESCE(scope, 'personal') = ?)"
        p.append(perspective)
    elif perspective == "total":
        q += " AND COALESCE(internal, '') != 'cross'"
        if kinds and "expense" in kinds:
            kinds = list(kinds) + ["distribution"]  # withholding tax on dividends is a real outflow for the family
    if date_from:
        q += " AND booking_date >= ?"
        p.append(date_from.isoformat())
    if date_to:
        q += " AND booking_date <= ?"
        p.append(date_to.isoformat())
    if kinds:
        q += f" AND kind IN ({','.join('?' * len(kinds))})"
        p += list(kinds)
    if accounts:
        q += f" AND account_id IN ({','.join('?' * len(accounts))})"
        p += list(accounts)
    if not include_pending:
        q += " AND status='booked'"
    return [dict(r) for r in conn.execute(q + " ORDER BY booking_date, id", p)]


def resolve_accounts(conn, account: str | None) -> list[str] | None:
    if not account:
        return None
    a = normalize_text(account)
    rows = conn.execute("SELECT id, name, iban_masked, bank, account_type FROM accounts WHERE source != 'asset'").fetchall()
    hits = [r["id"] for r in rows if a in normalize_text(r["name"]) or a == normalize_text(r["account_type"])
            or (r["iban_masked"] and r["iban_masked"][-4:] in a) or a in normalize_text(r["bank"] or "")]
    if not hits:
        raise ValueError(f"No account matches '{account}'. Use list_accounts to see names.")
    return hits


# --- balances ----------------------------------------------------------------------

def latest_balance(conn, account_id: str) -> dict:
    acc = conn.execute("SELECT id, iban, source, link_id FROM accounts WHERE id=?", (account_id,)).fetchone()
    if acc is not None and acc["source"] == "manual":
        from .manual import estimate
        e = estimate(conn, acc)
        return {"booked": (e["amount"], e["as_of"])} if e else {}
    out = {}
    for kind in ("booked", "available"):
        r = conn.execute("SELECT amount, as_of FROM balances WHERE account_id=? AND balance_type=? "
                         "ORDER BY as_of DESC LIMIT 1", (account_id, kind)).fetchone()
        if r:
            out[kind] = (r["amount"], r["as_of"])
    return out


def accounts_overview(conn) -> list[dict]:
    out = []
    for a in conn.execute("SELECT * FROM accounts WHERE source != 'asset' "
                          "ORDER BY COALESCE(scope,'personal') DESC, account_type, name"):
        b = latest_balance(conn, a["id"])
        stats = conn.execute("SELECT COUNT(*), MIN(booking_date), MAX(booking_date) FROM transactions WHERE account_id=?",
                             (a["id"],)).fetchone()
        last_sync = conn.execute("SELECT finished_at FROM sync_log WHERE account_id=? AND status='ok' ORDER BY id DESC LIMIT 1",
                                 (a["id"],)).fetchone()
        out.append({
            "name": a["name"], "bank": a["bank"], "type": a["account_type"], "scope": a["scope"] or "personal",
            "account": a["iban_masked"],
            "currency": a["currency"], "balance": r2(b["booked"][0]) if "booked" in b else None,
            "available": r2(b["available"][0]) if "available" in b else None,
            "balance_date": (b.get("available") or b.get("booked") or (None, None))[1],
            "credit_limit": a["credit_limit"], "source": a["source"], "last_sync": last_sync[0][:16] if last_sync else None,
            "transactions": stats[0], "history_from": stats[1], "history_to": stats[2],
        })
        if a["source"] == "manual":
            from .manual import estimate
            e = estimate(conn, a) or {}
            out[-1].update({"estimated": True, "balance_entered": e.get("entered"),
                            "transfers_since_entry": e.get("transfers_since"),
                            "history_from": None, "history_to": None,
                            "note": "Not shared by the bank: your last entered balance plus transfers seen since then "
                                    "(interest not included)."})
    return out


def _usable_balance(conn, account_id) -> tuple[float | None, str | None]:
    b = latest_balance(conn, account_id)
    v = b.get("available") or b.get("booked")
    return (v[0], v[1]) if v else (None, None)


def totals(conn, scope: str | None = None) -> dict:
    """Balances per account type; with scope=None also split into personal and business."""
    t = defaultdict(float)
    by_scope = defaultdict(float)
    as_of = None
    missing = []
    q = "SELECT * FROM accounts WHERE source != 'asset'" + (" AND COALESCE(scope,'personal') = ?" if scope else "")
    for a in conn.execute(q, (scope,) if scope else ()):
        b = latest_balance(conn, a["id"])
        v = b.get("booked") or b.get("available")
        if not v:
            missing.append(a["name"])
            continue
        t[a["account_type"]] += v[0]
        by_scope[a["scope"] or "personal"] += v[0]
        as_of = max(as_of or v[1], v[1])
    out = {k: r2(v) for k, v in t.items()}
    out["net_cash"] = r2(sum(t.values()))
    if not scope:
        out["personal"] = r2(by_scope.get("personal", 0))
        out["business"] = r2(by_scope.get("business", 0))
    out["as_of"] = as_of
    if missing:
        out["accounts_without_balance"] = missing
    return out


# --- spending ----------------------------------------------------------------------

def breakdown(conn, date_from: date, date_to: date, group_by: str = "category", kind: str = "expense",
              accounts=None, include_pending: bool = True, perspective: str | None = "personal") -> dict:
    rows = fetch(conn, date_from, date_to, kinds=[kind], accounts=accounts, include_pending=include_pending,
                 perspective=perspective)
    sign = 1 if kind == "income" else -1  # expenses, distributions and transfers: money out is positive
    agg = defaultdict(lambda: {"amount": 0.0, "count": 0})
    names = account_names(conn)
    for r in rows:
        if group_by == "category":
            key = r["category"]
        elif group_by == "group":
            key = GROUP.get(r["category"], "Other")
        elif group_by == "merchant":
            key = r["merchant"] or "Unknown"
        elif group_by == "month":
            key = r["booking_date"][:7]
        elif group_by == "week":
            d = _d(r["booking_date"])
            key = (d - timedelta(days=d.weekday())).isoformat()
        elif group_by == "account":
            key = names.get(r["account_id"], r["account_id"])
        else:
            raise ValueError("group_by must be category, group, merchant, month, week or account")
        agg[key]["amount"] += sign * r["amount"]
        agg[key]["count"] += 1
    total = sum(v["amount"] for v in agg.values())
    items = [{"key": k, "amount": r2(v["amount"]), "count": v["count"],
              "share": round(v["amount"] / total, 3) if total else None} for k, v in agg.items()]
    items.sort(key=lambda x: x["key"] if group_by in ("month", "week") else -x["amount"])
    return {"from": date_from.isoformat(), "to": date_to.isoformat(), "perspective": perspective, "kind": kind,
            "group_by": group_by, "total": r2(total), "items": items}


def cashflow(conn, months: int = 12, today: date | None = None, accounts=None,
             perspective: str | None = "personal") -> list[dict]:
    today = today or date.today()
    start = add_months(today.replace(day=1), -(months - 1))
    rows = fetch(conn, start, today, accounts=accounts, perspective=perspective)
    m = defaultdict(lambda: {"income": 0.0, "spending": 0.0, "to_savings_investments": 0.0, "distributions": 0.0})
    for r in rows:
        k = r["booking_date"][:7]
        if r["kind"] == "income":
            m[k]["income"] += r["amount"]
        elif r["kind"] == "expense" or (r["kind"] == "distribution" and perspective == "total"):
            m[k]["spending"] -= r["amount"]
        elif r["kind"] == "distribution":
            m[k]["distributions"] -= r["amount"]
        elif r["category"] == "Savings & investments":
            m[k]["to_savings_investments"] -= r["amount"]
    out = []
    d = start
    while d <= today:
        k = d.isoformat()[:7]
        v = m[k]
        row = {"month": k, "income": r2(v["income"]), "spending": r2(v["spending"]),
               "net": r2(v["income"] - v["spending"]), "to_savings_investments": r2(v["to_savings_investments"]),
               "partial": k == today.isoformat()[:7]}
        if perspective == "business":
            row["dividends_paid"] = r2(v["distributions"])
        out.append(row)
        d = add_months(d, 1)
    return out


# --- recurring -----------------------------------------------------------------------

def _cadence(interval: float):
    for c in CADENCES:
        if c[2] <= interval <= c[3]:
            return c
    return None


def _close(a: float, b: float, pct: float = 0.03) -> bool:
    return abs(a - b) <= max(0.05, pct * max(abs(a), abs(b)))


# Payments whose amount depends on what you earned or spent, not on a price: never reported as a price increase.
# (BTW is turnover minus deductible VAT on costs; VenB and personal tax are advance payments or assessments.)
AMOUNT_VARIES = {"VAT", "Corporate tax", "Withholding tax", "Taxes & social security", "Tax refund"}


def _last_price_change(amounts: list[float], dates: list[date]) -> dict | None:
    """Most recent step change to a new level that has held (or a single big jump after a stable run)."""
    for i in range(len(amounts) - 1, 0, -1):
        new, old = amounts[i], amounts[i - 1]
        if not old or abs(new - old) < 0.5 or abs(new - old) / old < 0.05:
            continue
        tail_ok = all(_close(a, new) for a in amounts[i:])
        held = len(amounts) - i >= 2 and tail_ok
        stable_before = i >= 2 and _close(amounts[i - 2], old)
        if held or (tail_ok and stable_before and abs(new - old) / old >= 0.10):
            return {"from": r2(old), "to": r2(new), "pct": round((new - old) / old * 100, 1),
                    "since": dates[i].isoformat()}
        return None
    return None


def _scopes(conn) -> dict:
    return {r[0]: (r[1] or "personal") for r in conn.execute("SELECT id, scope FROM accounts")}


def recurring(conn, today: date | None = None, lookback_days: int = 460, include_income: bool = True,
              include_inactive: bool = False, perspective: str | None = None) -> list[dict]:
    today = today or date.today()
    rows = fetch(conn, today - timedelta(days=lookback_days), today, include_pending=False, perspective=perspective)
    scopes = _scopes(conn)
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for r in rows:
        if r["kind"] == "income" and not include_income:
            continue
        if r["category"] == "Own accounts" and not (r["amount"] < 0 and r["payment_type"] == "standing_order"):
            continue
        groups[(r["merchant_key"], r["category"], r["amount"] < 0)].append(r)

    out = []
    for (key, _cat, is_debit), items in groups.items():
        if key in (None, "unknown"):
            continue
        is_dd = any(i["payment_type"] in ("direct_debit", "standing_order") for i in items)
        category = statistics.mode([i["category"] for i in items])
        if category in HABITUAL and not is_dd:
            continue
        by_day = defaultdict(float)
        for i in items:
            by_day[i["booking_date"]] += abs(i["amount"])
        dates = sorted(by_day)
        if len(dates) < 2:
            continue
        ds = [_d(x) for x in dates]
        intervals = [(b - a).days for a, b in zip(ds, ds[1:])]
        med = statistics.median(intervals)
        cad = _cadence(med)
        if not cad:
            continue
        name, nominal, _, _, tol = cad
        if len(dates) == 2 and not (is_dd or name in ("quarterly", "every 6 months", "yearly")):
            continue
        regular = sum(1 for iv in intervals if abs(iv - nominal) <= tol + (3 if name == "monthly" else 0)) / len(intervals)
        if regular < 0.66:
            continue
        amounts = [by_day[x] for x in dates]
        mean = statistics.fmean(amounts)
        cv = statistics.pstdev(amounts) / mean if mean else 1
        if not (cv <= 0.25 or (is_dd and cv <= 0.75) or (len(amounts) >= 3 and max(amounts[-3:]) / max(min(amounts[-3:]), 0.01) <= 1.3)):
            continue
        last_date = ds[-1]
        if name == "monthly":
            day = round(statistics.median([d.day for d in ds[-4:]]))
            nxt = add_months(last_date, 1, day)
        else:
            nxt = last_date + timedelta(days=round(nominal))
        active = (today - last_date).days <= nominal + tol * 2
        if not active and not include_inactive:
            continue
        typical = statistics.median(amounts[-3:])
        change = _last_price_change(amounts, ds) if is_debit and category not in AMOUNT_VARIES else None
        sample = items[-1]
        out.append({
            "merchant": sample["merchant"], "category": category, "kind": sample["kind"],
            "direction": "out" if is_debit else "in", "cadence": name, "typical_amount": r2(typical),
            "last_amount": r2(amounts[-1]), "last_date": last_date.isoformat(),
            "next_expected": nxt.isoformat() if active else None, "occurrences": len(dates),
            "paid_by": "direct debit" if any(i["payment_type"] == "direct_debit" for i in items) else
                       "standing order" if any(i["payment_type"] == "standing_order" for i in items) else
                       sample["payment_type"],
            "monthly_equivalent": r2(typical * 30.44 / nominal), "price_change": change, "active": active,
            "scope": scopes.get(sample["account_id"], "personal"), "account_id": sample["account_id"],
            "merchant_key": key,
        })
    out.sort(key=lambda x: (x["direction"], -x["monthly_equivalent"]))
    return out


def upcoming(conn, days: int = 30, today: date | None = None, include_transfers: bool = False,
             perspective: str | None = None) -> dict:
    today = today or date.today()
    end = today + timedelta(days=days)
    items = []
    for rc in recurring(conn, today, include_income=True, perspective=perspective):
        if not rc["next_expected"]:
            continue
        if rc["kind"] == "transfer" and not include_transfers:
            continue
        nominal = next(c[1] for c in CADENCES if c[0] == rc["cadence"])
        tol = next(c[4] for c in CADENCES if c[0] == rc["cadence"])
        d = _d(rc["next_expected"])
        n = 0
        while d <= end and n < 60:
            if d >= today - timedelta(days=tol):
                items.append({"date": max(d, today).isoformat(), "expected_on": d.isoformat(),
                              "late": d < today, "merchant": rc["merchant"], "category": rc["category"],
                              "amount": rc["typical_amount"] if rc["direction"] == "in" else -rc["typical_amount"],
                              "cadence": rc["cadence"], "paid_by": rc["paid_by"], "kind": rc["kind"],
                              "scope": rc["scope"]})
            d = add_months(d, 1) if rc["cadence"] == "monthly" else d + timedelta(days=round(nominal))
            n += 1
    items.sort(key=lambda x: (x["date"], x["amount"]))
    out_total = sum(-i["amount"] for i in items if i["amount"] < 0)
    in_total = sum(i["amount"] for i in items if i["amount"] > 0)
    return {"from": today.isoformat(), "to": end.isoformat(), "total_out": r2(out_total), "total_in": r2(in_total),
            "items": items}


# --- unusual activity -----------------------------------------------------------------

_FX = re.compile(r"wisselkoers|exchange rate|taux de change|koers|\bfx\b")


def unusual(conn, days: int = 60, today: date | None = None, perspective: str | None = None) -> list[dict]:
    today = today or date.today()
    start = today - timedelta(days=days)
    window = fetch(conn, start, today, kinds=["expense"], perspective=perspective)
    history = fetch(conn, start - timedelta(days=365), start - timedelta(days=1), kinds=["expense"], include_pending=False,
                    perspective=perspective)
    scopes = _scopes(conn)
    alerts = []
    hist_by_key = defaultdict(list)
    for h in history:
        if h["amount"] < 0:
            hist_by_key[h["merchant_key"]].append(-h["amount"])
    all_debits = sorted(-h["amount"] for h in history if h["amount"] < 0)
    p95 = all_debits[int(len(all_debits) * 0.95)] if len(all_debits) >= 20 else 250.0

    def alert(kind, r, detail, severity="info", facts=None):
        alerts.append({"type": kind, "severity": severity, "date": r["booking_date"], "merchant": r["merchant"],
                       "amount": r2(r["amount"]), "category": r["category"], "detail": detail, "transaction_id": r["id"],
                       "scope": scopes.get(r["account_id"], "personal"), "facts": facts})

    rec = recurring(conn, today, include_income=False, perspective=perspective)
    recurring_fee_keys = {x["merchant_key"] for x in rec if x["category"] == "Bank fees"}
    debits = [r for r in window if r["amount"] < 0]
    for r in debits:
        amt = -r["amount"]
        if r["category"] == "Bank fees" and r["merchant_key"] not in recurring_fee_keys:
            alert("fee", r, "Bank fee or interest charge outside your regular account fees", "warning")
        if r["currency"] and r["currency"] != "EUR" or _FX.search(normalize_text(f"{r['description']} {r['remittance']}")):
            alert("foreign_currency", r, "Foreign-currency payment: exchange-rate costs may apply")
        hist = hist_by_key.get(r["merchant_key"], [])
        if len(hist) >= 3:
            med = statistics.median(hist)
            if amt > max(2.5 * med, med + 75):
                alert("higher_than_usual", r, f"About {amt / med:.1f}× your usual {med:.2f} at this merchant", "warning",
                      {"ratio": round(amt / med, 1), "usual": r2(med)})
        elif not hist and amt >= max(250.0, p95) and r["category"] not in (
                "Loans & mortgage", "Rent", "Taxes & social security", "VAT", "Corporate tax", "Withholding tax"):
            alert("large_new_merchant", r, "Large payment to a merchant you haven't paid in the past year")
    # Possible double charges: same merchant and amount within a day, reported once per group.
    clusters = defaultdict(list)
    for r in debits:
        if -r["amount"] >= 5 and r["category"] not in ("Public transport & taxi", "Parking & tolls"):
            clusters[(r["account_id"], r["merchant_key"], round(r["amount"], 2))].append(r)
    for items in clusters.values():
        items.sort(key=lambda x: x["booking_date"])
        group = [items[0]]
        for r in items[1:] + [None]:
            if r is not None and (_d(r["booking_date"]) - _d(group[-1]["booking_date"])).days <= 1:
                group.append(r)
                continue
            if len(group) >= 2:
                first = group[0]
                when = first["booking_date"] if group[-1]["booking_date"] == first["booking_date"] else \
                    f"{first['booking_date']} to {group[-1]['booking_date']}"
                alert("possible_duplicate", first, f"{len(group)} identical payments of {-first['amount']:.2f} to "
                                                   f"{first['merchant']} on {when}", "warning",
                      {"count": len(group), "until": group[-1]["booking_date"]})
            group = [r] if r is not None else []
    horizon = max(days, 180)
    for rc in rec:
        pc = rc["price_change"]
        if pc and pc["pct"] > 0 and rc["kind"] == "expense" and (today - _d(pc["since"])).days <= horizon:
            yearly = (pc["to"] - pc["from"]) * 365 / next(c[1] for c in CADENCES if c[0] == rc["cadence"])
            alerts.append({"type": "price_increase", "severity": "warning", "date": pc["since"],
                           "merchant": rc["merchant"], "amount": -rc["last_amount"], "category": rc["category"],
                           "detail": f"{rc['cadence'].capitalize()} payment went from {pc['from']:.2f} to {pc['to']:.2f} "
                                     f"(+{pc['pct']}%) since {pc['since']}, about {yearly:.0f} more per year",
                           "transaction_id": None, "scope": rc["scope"],
                           "facts": {"from": pc["from"], "to": pc["to"], "pct": pc["pct"], "since": pc["since"],
                                     "per_year": round(yearly), "cadence": rc["cadence"]}})
    order = {"warning": 0, "info": 1}
    alerts.sort(key=lambda a: (order[a["severity"]], a["date"]), reverse=False)
    return alerts


# --- payday -------------------------------------------------------------------------

def until_payday(conn, today: date | None = None, payday_day: int | None = None) -> dict:
    """Personal accounts only."""
    today = today or date.today()
    rec = recurring(conn, today, include_income=True, perspective="personal")
    salary = [r for r in rec if r["direction"] == "in" and r["cadence"] == "monthly"
              and r["category"] in ("Salary", "Director remuneration")] or \
             [r for r in rec if r["direction"] == "in" and r["cadence"] == "monthly" and r["typical_amount"] >= 500]
    salary.sort(key=lambda r: -r["typical_amount"])
    basis = None
    if payday_day:
        nxt = today.replace(day=min(payday_day, calendar.monthrange(today.year, today.month)[1]))
        if nxt <= today:
            nxt = add_months(today, 1, payday_day)
        basis = f"payday set to day {payday_day}"
    elif salary:
        nxt = _d(salary[0]["next_expected"])
        while nxt <= today:
            nxt = add_months(nxt, 1)
        basis = f"detected from {salary[0]['merchant']} ({salary[0]['cadence']}, ~{salary[0]['typical_amount']:.2f})"
    else:
        nxt = add_months(today.replace(day=1), 1)
        basis = "no regular salary detected; assumed the 1st of next month (pass payday_day to override)"
    days_left = max((nxt - today).days, 1)

    current = conn.execute("SELECT id, name FROM accounts WHERE account_type='current' "
                           "AND COALESCE(scope,'personal')='personal'").fetchall()
    available, as_of, unknown = 0.0, None, []
    for a in current:
        v, d = _usable_balance(conn, a["id"])
        if v is None:
            unknown.append(a["name"])
            continue
        available += v
        as_of = max(as_of or d, d)
    cards = []
    for a in conn.execute("SELECT id, name, credit_limit FROM accounts WHERE account_type='card' "
                          "AND COALESCE(scope,'personal')='personal'"):
        b = latest_balance(conn, a["id"]).get("booked")
        if b:
            cards.append({"card": a["name"], "balance": r2(b[0]), "credit_limit": a["credit_limit"]})

    up = upcoming(conn, days=days_left, today=today, include_transfers=True, perspective="personal")
    bills = [i for i in up["items"] if i["amount"] < 0 and _d(i["date"]) < nxt]
    bills_total = sum(-i["amount"] for i in bills if i["kind"] != "transfer")
    planned_transfers = sum(-i["amount"] for i in bills if i["kind"] == "transfer")

    recurring_keys = {r["merchant_key"] for r in rec}
    past = fetch(conn, today - timedelta(days=90), today - timedelta(days=1), kinds=["expense"], perspective="personal")
    one_offs = [r for r in past if r["merchant_key"] not in recurring_keys and -r["amount"] > 500]
    discretionary = sum(-r["amount"] for r in past if r["merchant_key"] not in recurring_keys and -r["amount"] <= 500)
    daily = discretionary / 90
    left = available - bills_total - planned_transfers
    return {
        "today": today.isoformat(), "next_payday": nxt.isoformat(), "days_left": days_left, "payday_basis": basis,
        "available_now": r2(available), "balance_as_of": as_of, "accounts_without_balance": unknown or None,
        "bills_before_payday": r2(bills_total), "planned_transfers_before_payday": r2(planned_transfers),
        "bills": [{k: b[k] for k in ("date", "merchant", "amount", "category")} for b in bills],
        "left_after_bills": r2(left), "safe_to_spend_per_day": r2(left / days_left),
        "your_usual_daily_spend": r2(daily), "projected_day_to_day_spend": r2(daily * days_left),
        "projected_at_payday": r2(left - daily * days_left), "credit_cards": cards or None,
        "excluded_one_offs_from_daily_average": [{"date": r["booking_date"], "merchant": r["merchant"],
                                                  "amount": r2(r["amount"])} for r in one_offs] or None,
    }


# --- overview -------------------------------------------------------------------------

def overview(conn, today: date | None = None, perspective: str = "personal") -> dict:
    _check_perspective(perspective)
    today = today or date.today()
    m0 = today.replace(day=1)
    p0 = add_months(m0, -1)
    p_same = add_months(today, -1)
    this = breakdown(conn, m0, today, perspective=perspective)
    prev_to_date = breakdown(conn, p0, p_same, perspective=perspective)
    prev_full = breakdown(conn, p0, m0 - timedelta(days=1), perspective=perspective)
    income = breakdown(conn, m0, today, kind="income", perspective=perspective)
    up_persp = None if perspective == "total" else perspective
    up = upcoming(conn, 14, today, perspective=up_persp)
    alerts = unusual(conn, 30, today, perspective=up_persp)
    last_tx = conn.execute("SELECT MAX(booking_date) FROM transactions").fetchone()[0]
    return {
        "today": today.isoformat(), "month": m0.isoformat()[:7], "perspective": perspective,
        "balances": totals(conn, None if perspective == "total" else perspective),
        "this_month": {"spending": this["total"], "income": income["total"], "net": r2(income["total"] - this["total"]),
                       "top_categories": this["items"][:6]},
        "last_month_same_point": {"spending": prev_to_date["total"]},
        "last_month_total": {"spending": prev_full["total"]},
        "upcoming_14_days": {"bills_total": up["total_out"], "count": sum(1 for i in up["items"] if i["amount"] < 0),
                             "next": [i for i in up["items"] if i["amount"] < 0][:5]},
        "alerts": {"count": len(alerts), "warnings": [a for a in alerts if a["severity"] == "warning"][:5]},
        "data_up_to": last_tx,
    }


# --- household flows -------------------------------------------------------------------

_JOINT = re.compile(r"\s[-&+/]\s|\s(?:en|et|and)\s")


def person_names(conn) -> dict:
    """Token set -> display name for each account holder (one per person, preferring non-capitals)."""
    from .categorize import _tokens
    display: dict = {}
    for r in conn.execute("SELECT name, scope FROM accounts WHERE source != 'asset' ORDER BY id"):
        if (r["scope"] or "personal") == "personal" and not _JOINT.search(r["name"] or ""):
            key = _tokens(r["name"])
            if key not in display or (display[key].isupper() and not (r["name"] or "").isupper()):
                display[key] = r["name"]
    return display


def owner_labels(conn, per_account: bool = False) -> dict:
    """Account id -> who it belongs to: the holder, 'Joint (...)' for shared accounts, or the company.
    per_account=True adds the bank, so moves between your own accounts stay visible."""
    from .categorize import _tokens
    rows = [dict(r) for r in conn.execute("SELECT id, name, scope, bank, account_type FROM accounts "
                                          "WHERE source != 'asset' ORDER BY id")]
    display: dict = {}
    for r in rows:  # one display name per person, preferring the variant that isn't all capitals
        if (r["scope"] or "personal") == "personal" and not _JOINT.search(r["name"] or ""):
            key = _tokens(r["name"])
            if key not in display or (display[key].isupper() and not (r["name"] or "").isupper()):
                display[key] = r["name"]
    out = {}
    for r in rows:
        name = r["name"] or r["id"]
        bank = r.get("bank") or ""
        if per_account and bank and r.get("account_type") == "savings":
            bank += " spaar"
        if (r["scope"] or "personal") == "business":
            out[r["id"]] = name
        elif _JOINT.search(name):
            out[r["id"]] = f"Joint ({bank or name})" if per_account else f"Joint ({name})"
        else:
            person = display.get(_tokens(name), name)
            out[r["id"]] = f"{person} ({bank})" if per_account and bank else person
    return out


FLOW_CATEGORIES = ("Own accounts", "Household transfers", "Director remuneration", "Dividends",
                   "Company current account", "Rental income")


def household_flows(conn, date_from: date, date_to: date, by_month: bool = False) -> dict:
    """Who moved money to whom: your own accounts, the joint account, household members and your company.
    Investments are left out (see financial_position). Each linked-to-linked transfer is counted once."""
    from .categorize import _tokens
    owners = owner_labels(conn, per_account=True)
    scopes = _scopes(conn)
    iban_owner = {r[0]: owners[r[1]] for r in conn.execute("SELECT iban, id FROM accounts WHERE iban IS NOT NULL")}
    iban_scope = {r[0]: (r[1] or "personal") for r in conn.execute("SELECT iban, scope FROM accounts WHERE iban IS NOT NULL")}
    joint_tokens = {_tokens(r["name"]): "Joint" for r in conn.execute("SELECT name FROM accounts WHERE source != 'asset'")
                    if _JOINT.search(r["name"] or "")}
    person_tokens = person_names(conn)
    company = next((o for i, o in owners.items() if scopes.get(i) == "business"), "your company")

    q = (f"SELECT * FROM transactions WHERE booking_date >= ? AND booking_date <= ? AND status='booked' "
         f"AND category IN ({','.join('?' * len(FLOW_CATEGORIES))}) "
         f"AND account_id IN (SELECT id FROM accounts WHERE COALESCE(scope,'personal')='personal')")
    rows = conn.execute(q, (date_from.isoformat(), date_to.isoformat(), *FLOW_CATEGORIES)).fetchall()
    acc_iban = {r[0]: r[1] for r in conn.execute("SELECT id, iban FROM accounts")}
    # Debit sides of transfers between linked accounts, so the matching credit isn't counted twice. A credit whose
    # debit side is missing (e.g. before that account's history starts) is counted from the receiving side.
    debits = defaultdict(list)
    for r in conn.execute("SELECT account_id, counterparty_iban, amount, booking_date FROM transactions "
                          "WHERE amount < 0 AND counterparty_iban IS NOT NULL AND status='booked'"):
        debits[(acc_iban.get(r[0]), r[1], round(-r[2], 2))].append(_d(r[3]))
    agg = defaultdict(lambda: {"amount": 0.0, "count": 0, "what": set(), "months": defaultdict(float)})
    for r in rows:
        r = dict(r)
        if r["category"] == "Rental income" and r.get("internal") != "cross":
            continue
        me = owners.get(r["account_id"], r["account_id"])
        cp = r.get("counterparty_iban")
        if cp and cp in iban_owner:
            other = iban_owner[cp]
            if iban_scope.get(cp) == "personal" and r["amount"] > 0:
                mirror = debits.get((cp, acc_iban.get(r["account_id"]), round(r["amount"], 2)), [])
                if any(abs((d - _d(r["booking_date"])).days) <= 5 for d in mirror):
                    continue  # the sending side is in the data too: count this transfer once, on the debit
        elif r["category"] == "Household transfers":
            other = r["merchant"] or "household member"
        elif r.get("internal") == "cross" or r["category"] in ("Director remuneration", "Dividends"):
            other = company if r.get("internal") == "cross" else f"{company} (via {r['merchant']})"
        else:
            toks = _tokens(r.get("counterparty_name"))
            base = joint_tokens.get(toks) or person_tokens.get(toks) or me
            other = f"{base} - other account (not linked)"
        frm, to = (me, other) if r["amount"] < 0 else (other, me)
        if frm == to:
            continue
        v = agg[(frm, to)]
        v["amount"] += abs(r["amount"])
        v["count"] += 1
        v["what"].add(r["category"])
        v["months"][r["booking_date"][:7]] += abs(r["amount"])
    flows = []
    for (frm, to), v in sorted(agg.items(), key=lambda kv: -kv[1]["amount"]):
        f = {"from": frm, "to": to, "amount": r2(v["amount"]), "count": v["count"], "what": sorted(v["what"])}
        if by_month:
            f["by_month"] = {m: r2(a) for m, a in sorted(v["months"].items())}
        flows.append(f)
    with_tx = {r[0] for r in conn.execute("SELECT DISTINCT account_id FROM transactions")}
    joint = sorted({o for i, o in owners.items() if o.startswith("Joint (") and i in with_tx})
    joint_ids = {i: o for i, o in owners.items() if o.startswith("Joint (")}
    joint_summary = {}
    for j in joint:
        income = defaultdict(float)
        ids = [i for i, o in joint_ids.items() if o == j]
        for r in conn.execute("SELECT merchant, category, amount FROM transactions WHERE account_id IN "
                              f"({','.join('?' * len(ids))}) AND kind='income' AND booking_date >= ? "
                              "AND booking_date <= ? AND status='booked'",
                              (*ids, date_from.isoformat(), date_to.isoformat())):
            income[f"{r['merchant']} ({r['category']})"] += r["amount"]
        ins = defaultdict(float)
        outs = defaultdict(float)
        for f in flows:
            if f["to"] == j:
                ins[f["from"]] += f["amount"]
            if f["from"] == j:
                outs[f["to"]] += f["amount"]
        joint_summary[j] = {"transfers_in_from": {k: r2(v) for k, v in sorted(ins.items(), key=lambda kv: -kv[1])},
                            "transfers_out_to": {k: r2(v) for k, v in sorted(outs.items(), key=lambda kv: -kv[1])},
                            "income_paid_in": {k: r2(v) for k, v in sorted(income.items(), key=lambda kv: -kv[1])[:10]}}
    return {"from": date_from.isoformat(), "to": date_to.isoformat(), "flows": flows, "joint_accounts": joint_summary,
            "note": "Moves between people, accounts and your company. Not counted as income or spending, except "
                    "salary, dividends and rent from the company, which are income on the personal side."}


def _sum(rows, pred, sign=1) -> float:
    return r2(sign * sum(r["amount"] for r in rows if pred(r)))


def _by_category(rows, sign=-1, top=None) -> list[dict]:
    agg = defaultdict(float)
    for r in rows:
        agg[r["category"]] += sign * r["amount"]
    items = sorted(({"category": k, "amount": r2(v)} for k, v in agg.items() if round(v, 2)), key=lambda x: -x["amount"])
    return items[:top] if top else items


def _into_personal_asset(r) -> bool:
    """A company payment into a pension or savings contract of yours (e.g. IPT / groepsverzekering premiums)."""
    return bool(r.get("asset_id")) and r.get("internal") == "cross" and r["category"] not in FLOW_CATEGORIES + ("Rent",)


def position(conn, date_from: date | None = None, date_to: date | None = None, today: date | None = None) -> dict:
    """Total financial picture: cash per scope, money put into investments, loans, business result, personal result
    and what moved from the company to you. Cash-basis numbers from bank data, not accounting figures."""
    today = today or date.today()
    date_from = date_from or today.replace(month=1, day=1)
    date_to = date_to or today
    scopes = _scopes(conn)
    rows = fetch(conn, date_from, date_to)
    biz = [r for r in rows if scopes.get(r["account_id"]) == "business"]
    per = [r for r in rows if scopes.get(r["account_id"], "personal") == "personal"]

    # --- business (cash basis) ---
    revenue = _sum(biz, lambda r: r["kind"] == "income")
    vat = _sum(biz, lambda r: r["category"] == "VAT", -1)
    corp_tax = _sum(biz, lambda r: r["category"] == "Corporate tax", -1)
    remuneration = _sum(biz, lambda r: r["category"] == "Director remuneration", -1)
    costs = [r for r in biz if r["kind"] == "expense" and r["category"] not in ("VAT", "Corporate tax")]
    other_costs = r2(-sum(r["amount"] for r in costs if r["category"] != "Director remuneration"))
    dividends_paid = _sum(biz, lambda r: r["category"] == "Dividends", -1)
    withholding = _sum(biz, lambda r: r["category"] == "Withholding tax", -1)
    result_before_tax = r2(revenue - vat - remuneration - other_costs)
    business = {
        "revenue_incl_vat": revenue,
        "income_by_category": _by_category([r for r in biz if r["kind"] == "income"], 1),
        "vat_paid": vat,
        "director_remuneration_net": remuneration,
        "other_costs": other_costs,
        "costs_by_category": _by_category(costs, -1),
        "result_before_corporate_tax": result_before_tax,
        "corporate_tax_paid": corp_tax,
        "result_after_corporate_tax": r2(result_before_tax - corp_tax),
        "dividends_paid_net": dividends_paid,
        "withholding_tax_on_dividends": withholding,
        "current_account_with_you": _sum(biz, lambda r: r["category"] == "Company current account"),
        "net_put_into_investments": _sum(biz, lambda r: r["category"] == "Savings & investments", -1),
        "paid_into_your_pension": _sum(biz, _into_personal_asset, -1),
        "note": "Cash basis from bank data: VAT is netted via payments to the tax office; no depreciation, accruals "
                "or invoices not yet paid. Your accountant's figures will differ.",
    }

    # --- personal ---
    p_income = [r for r in per if r["kind"] == "income"]
    p_spend = [r for r in per if r["kind"] == "expense"]
    invested = _sum(per, lambda r: r["category"] == "Savings & investments", -1)
    personal = {
        "income": _sum(p_income, lambda r: True),
        "income_by_category": _by_category(p_income, 1),
        "spending": r2(-sum(r["amount"] for r in p_spend)),
        "spending_by_category": _by_category(p_spend, -1, top=12),
        "net_put_into_investments": invested,
        "left_after_spending_and_investing": r2(sum(r["amount"] for r in p_income) + sum(r["amount"] for r in p_spend)
                                                - invested),
        "uncategorised_share_of_spending": round(
            sum(-r["amount"] for r in p_spend if r["category"] == "Other expenses") /
            max(sum(-r["amount"] for r in p_spend), 0.01), 3),
    }

    # --- company -> you ---
    company_to_you = {
        "net_salary": _sum(per, lambda r: r["category"] == "Director remuneration"),
        "dividends_net": _sum(per, lambda r: r["category"] == "Dividends"),
        "office_rent": _sum(per, lambda r: r["category"] == "Rental income" and r.get("internal") == "cross"),
        "current_account_net": _sum(per, lambda r: r["category"] == "Company current account"),
        "into_your_pension": _sum(biz, _into_personal_asset, -1),
    }
    company_to_you["total"] = r2(sum(company_to_you.values()))

    # --- investments: accounts you added (value, contributions) + brokers not added yet (net deposited) ---
    from . import assets as AS
    asum = AS.summary(conn, today)
    investments = [{"destination": u["destination"], "net_deposited": u["net_deposited"], "since": u["since"],
                    "last": u["last"], "scope": u["scope"], "added": False} for u in asum["not_added"]]
    for a in asum["accounts"]:
        if a["type"] in AS.INVESTING_ASSET_TYPES and a.get("contributions") is not None:
            investments.append({"destination": a["name"], "net_deposited": a["contributions"],
                                "since": a.get("first_payment"), "last": a.get("last_payment"), "scope": a["scope"],
                                "added": True})
    investments.sort(key=lambda x: -x["net_deposited"])

    # --- loans: recurring repayments ---
    loans = [{"lender": rc["merchant"], "monthly": rc["monthly_equivalent"], "scope": rc["scope"],
              "next": rc["next_expected"]}
             for rc in recurring(conn, today, include_income=False)
             if rc["category"] == "Loans & mortgage" or "finance" in normalize_text(rc["merchant"] or "")
             or "leasing" in normalize_text(rc["merchant"] or "")]

    cash = totals(conn)
    inv_total = r2(sum(i["net_deposited"] for i in investments))
    at = asum["totals"]
    net_worth = r2((cash.get("net_cash") or 0) + (at["assets_net"] or 0))
    return {
        "period": {"from": date_from.isoformat(), "to": date_to.isoformat()},
        "cash": {"personal": cash.get("personal"), "business": cash.get("business"), "total": cash.get("net_cash"),
                 "as_of": cash.get("as_of"), "accounts": [{k: a[k] for k in ("name", "bank", "scope", "type", "balance",
                                                                             "available", "balance_date")}
                                                          for a in accounts_overview(conn)]},
        "investments": {
            "value": at["investments_value"], "contributions": at["investments_contributions"],
            "gain_on_valued_accounts": at["gain_on_valued_accounts"], "gain_pct": at["gain_pct"],
            "not_added_net_deposited": at["not_added_net_deposited"], "by_type": at["by_type"],
            "oldest_value": at["oldest_value"],
            "accounts": [{k: a.get(k) for k in ("name", "type", "provider", "scope", "value", "value_as_of", "estimated",
                                                 "stale", "contributions", "gain", "gain_pct", "value_basis",
                                                 "contributions_complete", "contributions_uncertain")}
                         for a in asum["accounts"]],
            "net_deposited_total": inv_total, "by_destination": investments,
            "note": "Value = what you entered for each account (from the broker, insurer or bank), moved along with "
                    "the money you put in or took out since; accounts without a value count at what you put in. "
                    "Brokers you haven't added yet count at net deposits (never below 0).",
        },
        "property": [{k: a.get(k) for k in ("name", "value", "value_as_of", "stale")}
                     for a in asum["accounts"] if a["type"] == "property"],
        "loans": loans,
        "loans_outstanding": [{k: a.get(k) for k in ("name", "provider", "scope", "outstanding", "value_as_of",
                                                     "paid_since_value", "stale")} for a in asum["loans"]],
        "net_worth": {"total": net_worth, "cash": cash.get("net_cash"), "investments": at["investments_value"],
                      "brokers_not_added": at["not_added_net_deposited"], "property": at["property"],
                      "possessions": at.get("possessions"),
                      "loans": r2(-(at["loans_outstanding"] or 0)), "by_scope": {
                          # a pension contract is yours, also when the company pays the premiums (r20)
                          s_: r2((cash.get(s_) or 0) + (at["by_scope"].get(s_) or 0)
                                 + (at.get("business_pension") or 0) * (1 if s_ == "personal" else -1)
                                 - sum(a.get("outstanding") or 0 for a in asum["loans"] if a["scope"] == s_))
                          for s_ in ("personal", "business")},
                      "without_value": at["accounts_without_value"]},
        "known_assets_total": net_worth,
        "business": business,
        "personal": personal,
        "company_to_you": company_to_you,
        "household_flows": household_flows(conn, date_from, date_to)["flows"][:12],
    }


def search(conn, query=None, category=None, merchant=None, account=None, date_from=None, date_to=None,
           min_amount=None, max_amount=None, direction=None, limit=50) -> dict:
    q, p = "SELECT * FROM transactions WHERE 1=1", []
    if query:
        like = f"%{query}%"
        q += (" AND (merchant LIKE ? OR counterparty_name LIKE ? OR description LIKE ? OR remittance LIKE ? "
              "OR category LIKE ? OR structured_ref LIKE ?)")
        p += [like] * 6
    if category:
        q += " AND category = ?"
        p.append(category)
    if merchant:
        q += " AND (merchant LIKE ? OR merchant_key = ?)"
        p += [f"%{merchant}%", normalize_text(merchant)]
    try:
        accs = resolve_accounts(conn, account)
    except ValueError:
        from .assets import find
        try:
            a = find(conn, account)
        except ValueError:
            raise ValueError(f"No account matches '{account}'. Use list_accounts or list_assets to see names.") from None
        q += " AND asset_id = ?"
        p.append(a["id"])
        accs = None
    if accs:
        q += f" AND account_id IN ({','.join('?' * len(accs))})"
        p += accs
    if date_from:
        q += " AND booking_date >= ?"
        p.append(str(date_from))
    if date_to:
        q += " AND booking_date <= ?"
        p.append(str(date_to))
    if min_amount is not None:
        q += " AND ABS(amount) >= ?"
        p.append(float(min_amount))
    if max_amount is not None:
        q += " AND ABS(amount) <= ?"
        p.append(float(max_amount))
    if direction in ("in", "out"):
        q += " AND amount > 0" if direction == "in" else " AND amount < 0"
    total = conn.execute(q.replace("SELECT *", "SELECT COUNT(*), COALESCE(SUM(amount),0)", 1), p).fetchone()
    rows = conn.execute(q + " ORDER BY booking_date DESC, id LIMIT ?", p + [int(min(max(limit, 1), 500))]).fetchall()
    names = account_names(conn)
    return {"matches": total[0], "sum": r2(total[1]), "returned": len(rows),
            "transactions": [tx_public(r, names) for r in rows]}


__all__ = ["accounts_overview", "breakdown", "cashflow", "recurring", "upcoming", "unusual", "until_payday",
           "overview", "position", "household_flows", "owner_labels", "search", "totals", "KIND", "PERSPECTIVES"]
