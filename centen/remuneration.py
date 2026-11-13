"""Salary or dividend (r18): how much of the company's profit ends up in your pocket, for different splits between
director's remuneration and a distribution, with the Belgian rules of income year 2026.

For a remuneration G (the cost for the company, social contributions included):
    social contributions  20,5 % up to € 75.024,54 and 14,16 % up to € 110.562,42 of the net professional income,
                          minimum on € 17.374,08, plus the fund's management fee
    professional income   G − contributions − 3 % lump-sum costs (max € 3.200)
    personal income tax   25 / 40 / 45 / 50 % brackets (16.720 / 29.510 / 51.070), tax-free amount € 11.550
                          (+ supplements for children), plus the municipal surcharge
For the rest of the profit P − G:
    corporate tax         20 % on the first € 100.000 when the remuneration is at least € 50.000, else 25 %
    then one of           an ordinary dividend (30 %), VVPRbis (18 %, only for shares from a cash contribution
                          that qualify), a liquidation reserve (10 % now + 9,8 % when paid out after 3 years, 0 % at
                          liquidation), or keeping it in the company.
A calculation aid with simplifying assumptions (no marriage quotient, no other income, no benefits in kind, warrants
counted as ordinary remuneration), not tax advice.
"""
from __future__ import annotations

import json
from datetime import date, timedelta

from . import analytics as A
from . import db

META_KEY = "tax_profile"
DEFAULTS = {"municipal_rate": 0.07, "children": 0, "vvprbis": False, "social_fee": 0.0305}

# Income year 2026
BRACKETS = [(16720.0, 0.25), (29510.0, 0.40), (51070.0, 0.45), (float("inf"), 0.50)]
TAX_FREE = 11550.0
CHILD_SUPPLEMENT = [0.0, 2030.0, 5240.0, 11720.0, 18970.0]  # cumulative for 0..4 children (indexed 2026); +7.250 more
LUMP_COSTS = (0.03, 3200.0)
SOCIAL = {"min_base": 17374.08, "t1": 75024.54, "r1": 0.205, "t2": 110562.42, "r2": 0.1416}
CIT = {"reduced": 0.20, "reduced_upto": 100000.0, "normal": 0.25, "min_remuneration": 50000.0}
DIVIDEND = {"ordinary": 0.30, "vvprbis": 0.18, "liq_now": 0.10, "liq_out": 0.098}


def r2(x):
    return None if x is None else round(float(x), 2)


def profile(conn) -> dict:
    p = dict(DEFAULTS)
    raw = db.get_meta(conn, META_KEY)
    if raw:
        try:
            p.update(json.loads(raw))
        except ValueError:
            pass
    return p


def set_profile(conn, municipal_rate: float | None = None, children: int | None = None,
                vvprbis: bool | None = None, social_fee: float | None = None) -> dict:
    p = profile(conn)
    if municipal_rate is not None:
        m = float(municipal_rate)
        m = m / 100 if m > 1 else m
        if not 0 <= m <= 0.12:
            raise ValueError("municipal_rate is a percentage between 0 and 12 (e.g. 7.5)")
        p["municipal_rate"] = m
    if children is not None:
        if not 0 <= int(children) <= 12:
            raise ValueError("children: 0 to 12")
        p["children"] = int(children)
    if vvprbis is not None:
        p["vvprbis"] = bool(vvprbis)
    if social_fee is not None:
        f = float(social_fee)
        p["social_fee"] = f / 100 if f > 0.2 else f
    db.set_meta(conn, META_KEY, json.dumps(p))
    conn.commit()
    return p


def tax_free_amount(children: int) -> float:
    extra = CHILD_SUPPLEMENT[min(children, 4)] + max(0, children - 4) * 7250.0
    return TAX_FREE + extra


def income_tax(taxable: float, children: int = 0, municipal: float = 0.07) -> float:
    def scale(x):
        t, lo = 0.0, 0.0
        for hi, rate in BRACKETS:
            if x > lo:
                t += (min(x, hi) - lo) * rate
            lo = hi
        return t
    base = max(0.0, scale(taxable) - scale(min(taxable, tax_free_amount(children))))
    return base * (1 + municipal)


def social_contributions(net_income: float, fee: float = 0.0305) -> float:
    s = SOCIAL
    b = max(net_income, s["min_base"])
    c = min(b, s["t1"]) * s["r1"] + max(0.0, min(b, s["t2"]) - s["t1"]) * s["r2"]
    return c * (1 + fee)


def remuneration_net(gross: float, children: int = 0, municipal: float = 0.07, fee: float = 0.0305) -> dict:
    """Net in pocket from a remuneration that costs the company `gross` (contributions included)."""
    if gross <= 0:
        return {"gross": 0.0, "social": 0.0, "costs": 0.0, "taxable": 0.0, "tax": 0.0, "net": 0.0}
    c = 0.0
    for _ in range(60):  # contributions are on the income after contributions: solve by iteration
        after = gross - c
        costs = min(after * LUMP_COSTS[0], LUMP_COSTS[1])
        nc = social_contributions(after - costs, fee)
        if abs(nc - c) < 0.005:
            break
        c = nc
    after = gross - c
    costs = min(after * LUMP_COSTS[0], LUMP_COSTS[1])
    taxable = after - costs
    t = income_tax(taxable, children, municipal)
    return {"gross": r2(gross), "social": r2(c), "costs": r2(costs), "taxable": r2(taxable), "tax": r2(t),
            "net": r2(gross - c - t)}


def corporate_tax(profit: float, remuneration: float) -> float:
    if profit <= 0:
        return 0.0
    if remuneration >= CIT["min_remuneration"]:
        low = min(profit, CIT["reduced_upto"])
        return low * CIT["reduced"] + (profit - low) * CIT["normal"]
    return profit * CIT["normal"]


def distribution(after_tax: float, vvprbis: bool) -> dict:
    """Net to you from the company's profit after corporate tax, per way of taking it out."""
    a = max(0.0, after_tax)
    reserve = a / (1 + DIVIDEND["liq_now"])
    out = {"ordinary": a * (1 - DIVIDEND["ordinary"]),
           "liquidation_reserve": reserve * (1 - DIVIDEND["liq_out"]),
           "liquidation_reserve_at_liquidation": reserve,
           "keep": 0.0}
    if vvprbis:
        out["vvprbis"] = a * (1 - DIVIDEND["vvprbis"])
    return {k: r2(v) for k, v in out.items()}


def scenario(profit: float, remuneration: float, p: dict) -> dict:
    rem = remuneration_net(remuneration, p["children"], p["municipal_rate"], p["social_fee"])
    left = profit - remuneration
    cit = corporate_tax(left, remuneration)
    after = left - cit
    dist = distribution(after, p["vvprbis"])
    best_now = max((k for k in dist if k not in ("keep", "liquidation_reserve_at_liquidation")),
                   key=lambda k: dist[k])
    total = rem["net"] + dist[best_now]
    return {"remuneration": r2(remuneration), "remuneration_net": rem["net"], "social": rem["social"],
            "income_tax": rem["tax"], "profit_after_remuneration": r2(left), "corporate_tax": r2(cit),
            "reduced_rate": remuneration >= CIT["min_remuneration"] and left > 0, "after_corporate_tax": r2(after),
            "distribution_net": dist, "best_distribution": best_now, "net_total": r2(total),
            "net_share": round(total / profit, 4) if profit > 0 else None,
            "total_tax": r2(profit - total)}


def from_data(conn, today: date | None = None) -> dict:
    """Profit before the director's pay and the current remuneration, last 12 months, cash basis."""
    today = today or date.today()
    rows = A.fetch(conn, today - timedelta(days=365), today, perspective="business", include_pending=False)
    revenue = sum(r["amount"] for r in rows if r["category"] == "Revenue")
    vat = sum(r["amount"] for r in rows if r["category"] == "VAT")
    owner = {"Director remuneration", "Dividends", "Withholding tax", "Corporate tax", "Own accounts",
             "Savings & investments", "Company current account", "VAT"}
    costs = sum(r["amount"] for r in rows if r["amount"] < 0 and r["category"] not in owner
                and r["kind"] in ("expense", "distribution"))
    rem = -sum(r["amount"] for r in rows if r["category"] == "Director remuneration" and r["amount"] < 0)
    profit = revenue + vat + costs  # vat and costs are negative
    return {"profit_before_remuneration": r2(profit), "remuneration_now": r2(rem), "revenue": r2(revenue),
            "vat": r2(vat), "costs": r2(costs), "from": (today - timedelta(days=365)).isoformat(),
            "to": today.isoformat()}


def compare(conn, today: date | None = None, profit: float | None = None, remuneration: float | None = None,
            step: float = 5000.0) -> dict:
    p = profile(conn)
    d = from_data(conn, today)
    P = profit if profit is not None else d["profit_before_remuneration"] or 0.0
    now = remuneration if remuneration is not None else d["remuneration_now"] or 0.0
    grid = sorted({round(x) for x in [0.0, now, CIT["min_remuneration"], P]
                   + [i * step for i in range(int(P // step) + 1)]} | set())
    grid = [g for g in grid if 0 <= g <= P]
    rows = [scenario(P, g, p) for g in grid]
    best = max(rows, key=lambda r: r["net_total"]) if rows else None
    cur = scenario(P, now, p) if P > 0 else None
    return {
        "profile": p, "data": d, "profit": r2(P), "remuneration_now": r2(now),
        "current": cur, "best": best, "gain_vs_current": r2(best["net_total"] - cur["net_total"]) if best and cur else None,
        "grid": rows,
        "rules_2026": {"brackets": BRACKETS[:3], "tax_free": TAX_FREE, "social": SOCIAL, "corporate_tax": CIT,
                       "dividend": DIVIDEND, "lump_costs": LUMP_COSTS},
        "notes": [
            "Kasbasis: winst = omzet − btw − kosten van de laatste 12 maanden, vóór je bezoldiging en de "
            "vennootschapsbelasting. Je boekhouder heeft de juiste cijfers; geef ze mee als je ze kent.",
            "Bezoldiging = kost voor de BV, sociale bijdragen inbegrepen; warrants tellen hier als gewone bezoldiging.",
            "Liquidatiereserve: 10 % bij aanleg, 9,8 % bij uitkering na 3 jaar, 0 % bij vereffening; het geld blijft "
            "intussen in de BV.",
            "VVPRbis (18 %) enkel voor aandelen uit een inbreng in geld die aan de voorwaarden voldoen" +
            (" (aangezet in je profiel)." if p["vvprbis"] else " (niet aangezet in je profiel)."),
            "Niet meegerekend: huwelijksquotiënt, andere inkomens, voordelen van alle aard, voorafbetalingen.",
        ],
    }
