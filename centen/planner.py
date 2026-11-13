"""Scenario planner (r17): where your wealth goes from here, year by year, and when it could carry your spending
(financial independence, "FIRE"). Starts from your real figures and lets you change the assumptions.

The model, per year and in today's euros (real return = return − inflation):
    capital = capital × (1 + real return) + saving while working
                                          − spending once you stop working
                                          + legal pension (and the pillar 2/3 capital) from the pension date
Spending falls when a mortgage is paid off (each loan's instalment stops at its maturity). Financially independent =
the first year in which capital × withdrawal rate covers that year's spending (minus the legal pension once it's
paid). The same engine runs in the dashboard's "Planner" tab, so sliders there give the same answers as this tool.
"""
from __future__ import annotations

from datetime import date, timedelta

from . import analytics as A
from . import assets as AS

DEFAULTS = {"return": 0.05, "inflation": 0.02, "withdrawal_rate": 0.035, "company_extraction_tax": 0.15,
            "include_company": True, "live_until": 90}


def r2(x):
    return None if x is None else round(float(x), 2)


def inputs(conn, today: date | None = None) -> dict:
    """The starting point taken from the data (every field can be overridden in `project`)."""
    today = today or date.today()
    from . import loans, pension
    pen = pension.overview(conn, today)
    prof = pen["profile"]
    tot = A.totals(conn)
    asum = AS.summary(conn, today)
    personal_inv = sum(AS.counted_value(a) for a in asum["accounts"]
                       if a["type"] in ("investment", "crypto", "insurance", "asset") and a["scope"] == "personal")
    company_inv = sum(AS.counted_value(a) for a in asum["accounts"]
                      if a["type"] in ("investment", "crypto", "insurance", "asset") and a["scope"] == "business")
    p23 = [x for x in pen["pillars"] if x["pillar"] in (2, 3)]
    p23_now = sum(x.get("value_now") or 0 for x in p23)
    p23_at_ret = sum(x.get("after_tax_today_eur") or 0 for x in p23)
    # Saving capacity: income − spending over the last 12 complete months, everything together (cash basis).
    flows = A.cashflow(conn, 13, today, perspective="total")
    full = [m for m in flows if not m["partial"]][-12:]
    saving_m = sum(m["net"] for m in full) / len(full) if full else 0.0
    # One-off purchases (a car, a renovation) aren't part of what you can save each month: add them back.
    offs_saving = 0.0
    if full:
        w0 = date.fromisoformat(full[0]["month"] + "-01")
        w1 = A.add_months(date.fromisoformat(full[-1]["month"] + "-01"), 1) - timedelta(days=1)
        offs_saving = sum(o["amount"] for o in pension.one_offs(conn, w0, w1)) / len(full)
        saving_m += offs_saving
    sp = pension.spending_profile(conn, today)
    spend_y = (sp["monthly"] or 0) * 12
    lo = loans.overview(conn, today) if conn.execute("SELECT 1 FROM loan_terms LIMIT 1").fetchone() else {"loans": []}
    loan_list = [{"name": x["account"], "instalment_monthly": x.get("instalment"), "ends": x.get("maturity_date"),
                  "outstanding": x.get("outstanding_now")} for x in lo.get("loans", [])]
    birth = prof.get("birth_date")
    age = (today - date.fromisoformat(birth)).days / 365.25 if birth else None
    return {
        "today": today.isoformat(), "age": round(age, 1) if age is not None else None, "birth_date": birth,
        "retirement_age": prof["retirement_age"], "live_until": prof.get("live_until", DEFAULTS["live_until"]),
        "cash_personal": r2(tot.get("personal")), "cash_company": r2(tot.get("business")),
        "investments_personal": r2(personal_inv), "investments_company": r2(company_inv),
        "pension_capital_now": r2(p23_now), "pension_capital_at_retirement_today_eur": r2(p23_at_ret),
        "stop_work_age": prof.get("stop_work_age") or prof["retirement_age"],
        # Pillar 2/3 accounts, so a scenario that stops working earlier also stops their premiums.
        "pension_accounts": [{"name": a["name"], "value": a["value"] or 0, "premium": a["premium_assumed"] or 0,
                              "return": a["return_assumed"], "tax": prof["payout_tax"] if x["pillar"] == 2 else 0.08,
                              "expected": a["expected_at_retirement"]}
                             for x in p23 for a in x["accounts"]],
        "legal_pension_monthly": prof.get("legal_pension_monthly"),
        "monthly_saving": r2(saving_m), "annual_spending": r2(spend_y),
        "one_offs_added_back_monthly": r2(offs_saving), "one_offs": sp["one_offs"],
        "loans": loan_list, "return": DEFAULTS["return"], "inflation": prof.get("inflation", DEFAULTS["inflation"]),
        "withdrawal_rate": prof.get("withdrawal_rate", DEFAULTS["withdrawal_rate"]),
        "company_extraction_tax": DEFAULTS["company_extraction_tax"], "include_company": True,
        "notes": "monthly_saving = income − spending of the last 12 complete months for you and the company together "
                 "(cash basis, before money put into investments, one-off purchases added back). annual_spending = "
                 "your personal spending profile (last 12 months without one-offs, or your own figure), loan "
                 "instalments included. Pension capital (pillars 2/3) is counted from the "
                 "pension date, after tax. Company money counts after the extraction tax.",
    }


def pension_capital(inp: dict, stop_age: float | None, inflation: float) -> float:
    """Pillar 2/3 capital at the pension date after tax, in today's euros, with premiums until stop_age."""
    accs = inp.get("pension_accounts")
    age0, ret_age = inp.get("age"), inp.get("retirement_age") or 67
    if accs is None or age0 is None:
        return inp.get("pension_capital_at_retirement_today_eur") or 0.0
    years = max(0.0, ret_age - age0)
    paying = max(0.0, min(years, (stop_age if stop_age is not None else ret_age) - age0))
    total = 0.0
    for a in accs:
        r = a["return"]
        g = (1 + r) ** paying
        fv = a["value"] * g + (a["premium"] * (g - 1) / r if r else a["premium"] * paying)
        fv *= (1 + r) ** (years - paying)
        if a.get("expected") and paying >= years - 0.05:
            fv = a["expected"]  # from the pension fiche, which assumes premiums until the pension
        total += fv / (1 + inflation) ** years * (1 - a["tax"])
    return total


def project(inp: dict, stop_work_age: float | None = None, extra_monthly_saving: float = 0.0,
            spending_change_pct: float = 0.0, return_: float | None = None, inflation: float | None = None,
            pay_off_loans_now: bool = False, withdrawal_rate: float | None = None, years: int | None = None) -> dict:
    """Year-by-year projection from `inputs` with the scenario changes."""
    today = date.fromisoformat(inp["today"])
    ret = inp["return"] if return_ is None else return_
    inf = inp["inflation"] if inflation is None else inflation
    wr = inp["withdrawal_rate"] if withdrawal_rate is None else withdrawal_rate
    real = (1 + ret) / (1 + inf) - 1
    company = 0.0
    if inp.get("include_company", True):
        company = ((inp.get("cash_company") or 0) + (inp.get("investments_company") or 0)) * \
                  (1 - inp.get("company_extraction_tax", 0.15))
    capital = (inp.get("cash_personal") or 0) + (inp.get("investments_personal") or 0) + company
    loans = [dict(x) for x in inp.get("loans") or [] if x.get("instalment_monthly")]
    if pay_off_loans_now:
        capital -= sum(x.get("outstanding") or 0 for x in loans)
        loans = []
    base_spend = (inp.get("annual_spending") or 0) * (1 + spending_change_pct)
    # Instalments are in today's spending; they stop at maturity (pay_off_loans_now: they stop right away).
    instal_now = sum((x["instalment_monthly"] or 0) * 12 for x in inp.get("loans") or [] if x.get("instalment_monthly"))
    if pay_off_loans_now:
        base_spend -= instal_now * (1 + spending_change_pct)
    saving = (inp.get("monthly_saving") or 0) * 12 + extra_monthly_saving * 12
    saving -= (inp.get("annual_spending") or 0) * spending_change_pct  # spending less means saving more
    if pay_off_loans_now:
        saving += instal_now  # the instalments you no longer pay are saved while you work
    age0 = inp.get("age")
    ret_age = inp.get("retirement_age") or 67
    end_age = inp.get("live_until") or 90
    n = years or (int(end_age - age0) + 1 if age0 is not None else 40)
    pension_y = (inp.get("legal_pension_monthly") or 0) * 12
    if stop_work_age is None:
        stop_work_age = inp.get("stop_work_age")
    if stop_work_age is not None and stop_work_age >= ret_age:
        stop_work_age = None
    pcap = pension_capital(inp, stop_work_age, inf)
    rows, fi_year, fi_age, depleted = [], None, None, None
    for i in range(n + 1):
        y = today.year + i
        age = age0 + i if age0 is not None else None
        ends = [x for x in loans if x.get("ends") and int(x["ends"][:4]) < y]
        spend = base_spend - sum((x["instalment_monthly"] or 0) * 12 * (1 + spending_change_pct) for x in ends)
        retired = age is not None and age >= ret_age
        working = not retired and (stop_work_age is None or age is None or age < stop_work_age)
        need = max(0.0, spend - (pension_y if retired else 0))
        if fi_year is None and capital > 0 and capital * wr >= need:
            fi_year, fi_age = y, (round(age, 1) if age is not None else None)
        rows.append({"year": y, "age": round(age, 1) if age is not None else None, "capital": r2(capital),
                     "spending": r2(spend), "phase": "pension" if retired else "work" if working else "living off capital"})
        if i == n:
            break
        freed = sum((x["instalment_monthly"] or 0) * 12 for x in ends)  # a paid-off loan's instalment is saved
        flow = saving + freed if working else -spend
        if retired:
            flow = pension_y - spend
        capital = capital * (1 + real) + flow
        if age is not None and age0 is not None and retired and abs(age - ret_age) < 1 and pcap:
            capital += pcap
        if capital < 0 and depleted is None:
            depleted = {"year": y + 1, "age": round(age + 1, 1) if age is not None else None}
    at_ret = next((r for r in rows if r["age"] is not None and r["age"] >= ret_age), None)
    return {"real_return": round(real, 4), "fi_year": fi_year, "fi_age": fi_age,
            "fi_target_now": r2(max(0.0, base_spend) / wr) if wr else None, "capital_now": rows[0]["capital"],
            "capital_at_retirement": at_ret["capital"] if at_ret else None, "depleted": depleted,
            "end_capital": rows[-1]["capital"], "years": rows}


SCENARIOS = [
    ("Zoals nu", {}),
    ("€ 500 per maand meer sparen", {"extra_monthly_saving": 500}),
    ("10 % minder uitgeven", {"spending_change_pct": -0.10}),
    ("Lager rendement (3 %)", {"return_": 0.03}),
    ("Leningen nu aflossen", {"pay_off_loans_now": True}),
    ("Stoppen met werken op 55", {"stop_work_age": 55}),
]


def plan(conn, today: date | None = None, **scenario) -> dict:
    inp = inputs(conn, today)
    overrides = {k: scenario.pop(k) for k in list(scenario) if k in inp and k not in ("today", "loans")}
    for k, v in overrides.items():
        if v is not None:
            inp[k] = v
    main = project(inp, **{k: v for k, v in scenario.items() if v is not None})
    compare = []
    for name, sc in SCENARIOS:
        if sc.get("stop_work_age") and (inp.get("age") is None or inp["age"] >= sc["stop_work_age"]):
            continue
        if sc.get("pay_off_loans_now") and not inp.get("loans"):
            continue
        p = project(inp, **sc)
        compare.append({"name": name, "changes": sc, "fi_year": p["fi_year"], "fi_age": p["fi_age"],
                        "capital_at_retirement": p["capital_at_retirement"], "depleted": p["depleted"],
                        "end_capital": p["end_capital"]})
    return {"inputs": inp, "projection": main, "scenarios": compare,
            "disclaimer": "Een planningsmodel met eenvoudige aannames in euro van vandaag, geen financieel advies. "
                          "Rendementen schommelen van jaar tot jaar: kijk naar de richting en de verschillen tussen "
                          "scenario's."}
