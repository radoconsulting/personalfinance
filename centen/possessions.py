"""Possessions (r19): the home, cars, watches, art — things you own that aren't investments. Per item the value now,
what it cost (the purchase price of a depreciation schedule, else the first value entered), the change since, the
value history, its share of your net worth and, for cars and the home, what owning it cost over the last 12 months.
"""
from __future__ import annotations

import re
from datetime import date, timedelta

from . import analytics as A
from . import assets as AS

KIND_RX = [
    ("Wagen", re.compile(r"porsche|bmw|audi|volkswagen|\bvw\b|tesla|mercedes|volvo|toyota|skoda|kia|hyundai|renault|"
                         r"peugeot|citro[eë]n|ford|opel|fiat|mini\b|lexus|jaguar|land ?rover|polestar|cupra|seat|"
                         r"ferrari|lamborghini|maserati|mclaren|\bauto\b|wagen|\bcar\b|motor|moto\b|camper|boot|boat", re.I)),
    ("Horloge & juwelen", re.compile(r"rolex|omega|patek|audemars|cartier|breitling|tudor|horloge|watch|juweel|"
                                     r"jewel|ring\b", re.I)),
    ("Kunst & verzameling", re.compile(r"kunst|schilderij|art\b|painting|verzameling|collect|wijn|wine|lego", re.I)),
]
COST_CATEGORIES = {
    "Wagen": ["Car & maintenance", "Fuel", "EV charging", "Parking & tolls"],
    "Woning": ["Home & DIY", "Energy", "Water"],
}


def r2(x):
    return None if x is None else round(float(x), 2)


def kind_of(acc: dict, has_schedule: bool) -> str:
    if acc["type"] == "property":
        return "Woning"
    text = f"{acc['name']} {acc.get('provider') or ''}"
    for k, rx in KIND_RX:
        if rx.search(text):
            return k
    return "Wagen" if has_schedule else "Andere"


def _history(conn, acc_id: str) -> list[dict]:
    rows = conn.execute("SELECT as_of, amount FROM balances WHERE account_id=? AND balance_type='valuation' "
                        "ORDER BY as_of", (acc_id,)).fetchall()
    return [{"date": r[0][:10], "value": r2(abs(r[1]))} for r in rows]


def running_costs(conn, today: date) -> dict:
    """What owning a car and the home cost over the last 12 months (personal accounts), without one-offs such as
    buying or selling the car itself."""
    from . import pension
    start = today - timedelta(days=365)
    offs = {o["id"] for o in pension.one_offs(conn, start, today)}
    out = {}
    for kind, cats in COST_CATEGORIES.items():
        per = {}
        for r in A.fetch(conn, start, today, kinds=["expense"], perspective="personal", include_pending=False):
            if r["category"] in cats and r["id"] not in offs:
                per[r["category"]] = per.get(r["category"], 0.0) - r["amount"]
        out[kind] = {"total": r2(sum(per.values())), "by_category": {k: r2(v) for k, v in sorted(per.items(),
                                                                                                key=lambda x: -x[1])},
                     "one_offs_left_out": r2(sum(o["amount"] for o in pension.one_offs(conn, start, today)
                                                 if o["category"] in cats))}
    return out


def overview(conn, today: date | None = None) -> dict:
    from . import depreciation, loans
    today = today or date.today()
    depreciation.ensure(conn)
    sched = {r["account_id"]: r for r in conn.execute("SELECT * FROM depreciation")}
    items = []
    for a in AS.list_accounts(conn, today):
        if a["type"] not in ("property", "asset"):
            continue
        acc = AS.find(conn, a["name"])
        hist = _history(conn, acc["id"])
        s = sched.get(acc["id"])
        value = AS.counted_value(a)
        if s is not None:
            cost, cost_date, basis = s["purchase_price"], s["purchase_date"], "aankoopprijs"
        elif hist:
            cost, cost_date, basis = hist[0]["value"], hist[0]["date"], "eerste waarde"
        else:
            cost, cost_date, basis = None, None, None
        months = [h for h in hist if h["date"].endswith(("-28", "-29", "-30", "-31")) or h is hist[0] or h is hist[-1]]
        items.append({
            "name": a["name"], "kind": kind_of(a, s is not None), "scope": a["scope"], "provider": a.get("provider"),
            "value": r2(value), "value_date": (a.get("value_entered") or {}).get("date"), "stale": a.get("stale", False),
            "cost": r2(cost), "cost_date": cost_date, "cost_basis": basis,
            "change": r2(value - cost) if cost is not None else None,
            "change_pct": round((value - cost) / cost, 4) if cost else None,
            "depreciation": {"yearly_rate": s["rate"], "per_month_next_year":
                             r2((depreciation.value_on(s, today) - depreciation.value_on(s, A.add_months(today, 12))) / 12)}
            if s is not None else None,
            "history": months,
        })
    order = {"Woning": 0, "Wagen": 1, "Horloge & juwelen": 2, "Kunst & verzameling": 3, "Andere": 4}
    items.sort(key=lambda x: (order.get(x["kind"], 9), -(x["value"] or 0)))
    lo = loans.overview(conn, today) if conn.execute("SELECT 1 FROM loan_terms LIMIT 1").fetchone() else None
    owed = sum(abs(a.get("outstanding") or 0) for a in AS.list_accounts(conn, today) if a["type"] == "loan")
    home_value = sum(i["value"] or 0 for i in items if i["kind"] == "Woning")
    total = sum(i["value"] or 0 for i in items)
    by_kind = {}
    for i in items:
        by_kind[i["kind"]] = r2(by_kind.get(i["kind"], 0) + (i["value"] or 0))
    nw = A.position(conn, today=today)["net_worth"]["total"] if items else None
    for i in items:
        i["share_of_net_worth"] = round(i["value"] / nw, 4) if nw and i["value"] else None
    return {
        "today": today.isoformat(), "items": items, "total": r2(total), "by_kind": by_kind,
        "home": {"value": r2(home_value), "loans": r2(owed), "equity": r2(home_value - owed) if home_value else None,
                 "loan_to_value": round(owed / home_value, 4) if home_value else None,
                 "interest_this_year": (lo or {}).get("this_year", {}).get("interest")},
        "without_home": r2(total - home_value), "net_worth": r2(nw),
        "share_of_net_worth": round(total / nw, 4) if nw else None,
        "running_costs_12m": running_costs(conn, today) if items else {},
        "notes": "Waarde = je laatste ingegeven waarde, of het afschrijvingsschema als je dat koos. Kosten = betalingen "
                 "van de laatste 12 maanden in die categorieën, zonder aan- of verkoop. Iets toevoegen: bijwerk-app → "
                 "Nieuwe rekening (soort ‘Andere bezitting’) of vraag het Claude.",
    }
