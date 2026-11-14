"""Private and company apart (r20): what is yours to spend versus what sits in the company.

Private: cash on personal accounts, personal investments, the pension (yours, but locked until the pension date),
the home minus the loans. Other possessions (a car, a watch) are listed but not counted (r25): only real estate is.
Company: cash on company accounts minus what it still owes the tax authorities (an estimate of the VAT of the
running quarter and this year's corporate tax not yet prepaid), company investments; and what that would leave you
after taking it out as a dividend (VVPRbis 18 % when the shares qualify, else 30 %).
"""
from __future__ import annotations

from datetime import date

from . import analytics as A
from . import assets as AS


def r2(x):
    return None if x is None else round(float(x), 2)


def _quarter_start(d: date) -> date:
    return date(d.year, 3 * ((d.month - 1) // 3) + 1, 1)


def vat_due(conn, today: date) -> dict:
    """VAT on the company's revenue received since the last quarter that was paid, at 21 % (output VAT only; the
    VAT on purchases you deduct is not known from the bank data)."""
    last = conn.execute("SELECT MAX(t.booking_date) FROM transactions t JOIN accounts a ON a.id=t.account_id "
                        "WHERE a.scope='business' AND t.category='VAT' AND t.amount<0").fetchone()[0]
    if last:
        paid_on = date.fromisoformat(last[:10])
        covered_until = _quarter_start(paid_on)  # a payment in month 1 of a quarter covers the previous quarter
        since = covered_until
    else:
        since = _quarter_start(today)
    rev = sum(r["amount"] for r in A.fetch(conn, since, today, perspective="business", include_pending=False)
              if r["category"] == "Revenue" and r["amount"] > 0)
    return {"since": since.isoformat(), "revenue_incl_vat": r2(rev), "amount": r2(rev * 21 / 121),
            "last_payment": last[:10] if last else None}


def corporate_tax_due(conn, today: date) -> dict:
    from . import remuneration as R
    start = date(today.year, 1, 1)
    rows = A.fetch(conn, start, today, perspective="business", include_pending=False)
    revenue = sum(r["amount"] for r in rows if r["category"] == "Revenue")
    vat = sum(r["amount"] for r in rows if r["category"] == "VAT")
    owner = {"Dividends", "Withholding tax", "Corporate tax", "Own accounts", "Savings & investments",
             "Company current account", "VAT"}
    costs = sum(r["amount"] for r in rows if r["amount"] < 0 and r["category"] not in owner
                and r["kind"] in ("expense", "distribution"))
    rem = -sum(r["amount"] for r in rows if r["category"] == "Director remuneration" and r["amount"] < 0)
    paid = -sum(r["amount"] for r in rows if r["category"] == "Corporate tax")
    result = revenue + vat + costs  # remuneration is among the costs
    yearly_rem = rem * 365.25 / max(30, (today - start).days + 1)
    tax = R.corporate_tax(max(0.0, result), yearly_rem)
    return {"year": today.year, "result_before_tax": r2(result), "tax_estimate": r2(tax), "prepaid": r2(paid),
            "amount": r2(max(0.0, tax - paid)), "reduced_rate": yearly_rem >= R.CIT["min_remuneration"]}


def pillars(P: dict, B: dict | None, biz: dict | None) -> dict:
    """The same total cut the other way (r24): day-to-day money, investments and wealth, pension.

    daily   = cash on the bank accounts (the company's after the VAT and corporate tax it still owes)
    wealth  = investments (private + company, incl. tak 21/23) + real estate − loans (possessions listed, not counted)
    pension = pension contracts (2nd/3rd pillar), locked until the pension date
    The three add up to the total of `split`.
    """
    owed = (biz or {}).get("owed_estimate") or 0.0
    b = B or {k: 0.0 for k in P}
    daily = {"cash_personal": P["cash"], "cash_business": b["cash"] if B else None,
             "owed_business": -owed if B else None}
    daily["value"] = P["cash"] + (b["cash"] - owed if B else 0.0)
    owned = P["property"] + b["property"]
    loans = P["loans"] + b["loans"]
    wealth = {"investments_personal": P["investments"], "investments_business": b["investments"] if B else None,
              "property": P["property"] + b["property"], "possessions_not_counted": P["possessions"] + b["possessions"],
              "loans": -loans, "owned_net": owned - loans}
    wealth["value"] = P["investments"] + b["investments"] + owned - loans
    pension = {"value": P["pension"]}
    out = {"daily": daily, "wealth": wealth, "pension": pension}
    return {k: {kk: r2(vv) for kk, vv in v.items()} for k, v in out.items()}


def split(conn, today: date | None = None) -> dict:
    from . import remuneration as R
    today = today or date.today()
    cash = A.totals(conn)
    accs = AS.summary(conn, today)
    P = {"cash": cash.get("personal") or 0.0, "investments": 0.0, "pension": 0.0, "possessions": 0.0,
         "property": 0.0, "loans": 0.0}
    B = {"cash": cash.get("business") or 0.0, "investments": 0.0, "pension": 0.0, "possessions": 0.0,
         "property": 0.0, "loans": 0.0}
    for a in accs["accounts"]:
        v = AS.counted_value(a)
        tgt = B if a["scope"] == "business" else P
        if a["type"] == "pension":
            P["pension"] += v  # a pension contract is the person's, also when the company pays the premiums
        elif a["type"] == "property":
            tgt["property"] += v
        elif a["type"] == "asset":
            tgt["possessions"] += v
        else:
            tgt["investments"] += v
    for u in accs["not_added"]:
        (B if u["scope"] == "business" else P)["investments"] += max(0.0, u["net_deposited"])
    for l in accs["loans"]:
        (B if l["scope"] == "business" else P)["loans"] += abs(l.get("outstanding") or 0)
    p_net = P["cash"] + P["investments"] + P["pension"] + P["property"] - P["loans"]  # r25: not the possessions
    out = {"personal": {k: r2(v) for k, v in P.items()}, "business": {k: r2(v) for k, v in B.items()}}
    out["personal"].update({
        "net_worth": r2(p_net),
        "available_now": r2(P["cash"] + P["investments"]),
        "locked": r2(P["pension"]),
        "owned": r2(P["property"] - P["loans"]),
    })
    has_bv = bool(conn.execute("SELECT 1 FROM accounts WHERE scope='business' LIMIT 1").fetchone())
    if has_bv:
        vat = vat_due(conn, today)
        cit = corporate_tax_due(conn, today)
        owed = (vat["amount"] or 0) + (cit["amount"] or 0)
        free = B["cash"] - owed
        b_net = free + B["investments"] + B["property"] - B["loans"]
        prof = R.profile(conn)
        rate = R.DIVIDEND["vvprbis"] if prof.get("vvprbis") else R.DIVIDEND["ordinary"]
        out["business"].update({
            "vat_due": vat, "corporate_tax_due": cit, "owed_estimate": r2(owed), "free_cash": r2(free),
            "net_worth": r2(b_net), "payout_rate": rate, "payout_route": "VVPRbis" if prof.get("vvprbis") else "dividend",
            "net_to_you": r2(max(0.0, b_net) * (1 - rate)),
        })
    else:
        out["business"] = None
    total = p_net + (out["business"]["net_worth"] if out["business"] else 0.0)
    out["total"] = r2(total)
    out["pillars"] = pillars(P, B if has_bv else None, out["business"])
    out["notes"] = ("Privé = van jou: vrij (liquide en beleggingen), vast tot je pensioen (pensioencontracten) en in je "
                    "woning en bezittingen min leningen. BV = van de vennootschap: pas van jou na een uitkering, en "
                    "dan na roerende voorheffing. ‘Nog te betalen’ is een schatting: btw aan 21 % op de omzet sinds het "
                    "laatst betaalde kwartaal (zonder aftrekbare btw) en de vennootschapsbelasting op het resultaat "
                    "van dit jaar min de voorafbetalingen.")
    return out
