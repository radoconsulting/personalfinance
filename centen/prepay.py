"""Pay off the mortgage early, or invest the money? (r18)

For an amount X you have now, per loan:
- the bank charges a reinvestment fee (wederbeleggingsvergoeding) of at most 3 months' interest on the amount repaid,
  so X pays off X / (1 + 3 × monthly rate) of capital;
- "shorter": the instalment stays, the loan ends earlier; "lower": the end date stays, the instalment drops;
- the comparison: on the loan's original end date, what do you have when you invest X now (and keep the loan), versus
  pay off now and invest what you no longer pay (the instalments after the new end date, or the lower instalment
  every month)? Gains on investments are taxed at the capital gains tax rate (10 % above the yearly exemption).
- break_even_return: the yearly return at which both are equal; above it investing wins.

Not included: a tax benefit on the loan (for loans from before 2020 the Flemish "geïntegreerde woonbonus" or the
federal "langetermijnsparen" can still apply: paying off then also stops that benefit), and the risk of investing.
The same calculation runs in the dashboard (Planner tab); keep both in step.
"""
from __future__ import annotations

from datetime import date

from . import loans

FEE_MONTHS = 3


def r2(x):
    return None if x is None else round(float(x), 2)


def _annuity(b: float, r: float, n: int) -> float:
    if n <= 0:
        return b
    return b / n if r == 0 else b * r / (1 - (1 + r) ** -n)


def _after_tax(value: float, paid_in: float, tax: float) -> float:
    return paid_in + max(0.0, value - paid_in) * (1 - tax)


def _payments(b: float, r: float, inst: float, n: int) -> list[float]:
    """Monthly payments to clear balance b with instalment inst, padded to n months (the last month also pays
    whatever is still open, as at the loan's end date)."""
    out = []
    for k in range(n):
        if b <= 0.005:
            out.append(0.0)
            continue
        b = b * (1 + r)
        pay = min(inst, b) if k < n - 1 else b
        b -= pay
        out.append(pay)
    return out


def _fv_diff(p0: list[float], p1: list[float], rm: float, tax: float) -> float:
    """Invest each month what you pay less in scenario 1; value at the end after tax on the gains."""
    n = len(p0)
    fv, paid_in = 0.0, 0.0
    for k, (a, b) in enumerate(zip(p0, p1)):
        d = a - b
        fv += d * (1 + rm) ** (n - 1 - k)
        paid_in += d
    return _after_tax(fv, paid_in, tax) if paid_in > 0 else fv


def calc(outstanding: float, monthly_rate: float, instalment: float, payments_left: int, amount: float,
         yearly_return: float, tax_on_gains: float = 0.10) -> dict:
    b, r, inst, n = float(outstanding), float(monthly_rate), float(instalment), int(payments_left)
    x = min(float(amount), b * (1 + FEE_MONTHS * r))
    paid = x / (1 + FEE_MONTHS * r)
    fee = x - paid
    b2 = max(0.0, b - paid)
    keep = _payments(b, r, inst, n)
    short = _payments(b2, r, inst, n)
    inst_low = _annuity(b2, r, n) if b2 > 0 else 0.0
    low = _payments(b2, r, inst_low, n)
    n_short = sum(1 for v in short if v > 0.005)

    def fv(ret, which):
        q = (1 + ret) ** (1 / 12) - 1
        inv = _after_tax(x * (1 + q) ** n, x, tax_on_gains)
        alt = _fv_diff(keep, short if which == "short" else low, q, tax_on_gains)
        return inv, alt

    def breakeven(which):
        g = lambda ret: fv(ret, which)[1] - fv(ret, which)[0]  # noqa: E731
        lo, hi = 0.0, 0.25
        if g(lo) < 0:
            return 0.0
        if g(hi) > 0:
            return None
        for _ in range(60):
            mid = (lo + hi) / 2
            lo, hi = (mid, hi) if g(mid) > 0 else (lo, mid)
        return round((lo + hi) / 2, 4)

    invest_now, short_fv = fv(yearly_return, "short")
    _, low_fv = fv(yearly_return, "low")
    return {
        "amount": r2(x), "capital_repaid": r2(paid), "reinvestment_fee": r2(fee), "outstanding_after": r2(b2),
        "loan_rate_yearly": round((1 + r) ** 12 - 1, 5), "months_left": n,
        "shorter": {"months_left": n_short, "months_earlier": n - n_short,
                    "interest_saved": r2(sum(keep) - x - sum(short)),
                    "fv_at_original_end": r2(short_fv), "advantage_vs_investing": r2(short_fv - invest_now),
                    "break_even_return": breakeven("short")},
        "lower": {"instalment": r2(inst_low), "instalment_drop": r2(inst - inst_low),
                  "interest_saved": r2(sum(keep) - x - sum(low)),
                  "fv_at_original_end": r2(low_fv), "advantage_vs_investing": r2(low_fv - invest_now),
                  "break_even_return": breakeven("low")},
        "invest_instead": {"fv_at_original_end": r2(invest_now), "yearly_return": yearly_return,
                           "tax_on_gains": tax_on_gains},
    }


def loans_for_calc(conn, today: date | None = None) -> list[dict]:
    out = []
    for x in loans.all_loans(conn, today):
        if not x.get("instalment") or not x.get("outstanding_now") or not x.get("payments_left"):
            continue
        out.append({"name": x["account"], "outstanding": x["outstanding_now"], "monthly_rate": x["monthly_rate"],
                    "instalment": x["instalment"], "payments_left": x["payments_left"],
                    "maturity_date": x["maturity_date"], "annual_rate": x["annual_rate_equivalent"]})
    return out


def analyse(conn, amount: float, loan: str | None = None, yearly_return: float = 0.05, tax_on_gains: float = 0.10,
            today: date | None = None) -> dict:
    ls = loans_for_calc(conn, today)
    if not ls:
        raise ValueError("No loan with a repayment plan yet (set_loan_terms).")
    if yearly_return > 1:
        yearly_return /= 100
    if loan:
        pick = [x for x in ls if x["name"].lower() == loan.lower()] or [x for x in ls if loan.lower() in x["name"].lower()]
        if not pick:
            raise ValueError(f"No loan called '{loan}'. Loans: {', '.join(x['name'] for x in ls)}")
    else:
        pick = sorted(ls, key=lambda x: -x["monthly_rate"])  # the dearest loan first
    res = []
    for x in pick:
        c = calc(x["outstanding"], x["monthly_rate"], x["instalment"], x["payments_left"], amount, yearly_return,
                 tax_on_gains)
        res.append({"loan": x["name"], "maturity_date": x["maturity_date"], "instalment_now": x["instalment"],
                    "outstanding_now": x["outstanding"], **c})
    best = res[0]
    better = "invest" if max(best["shorter"]["advantage_vs_investing"], best["lower"]["advantage_vs_investing"]) < 0 \
        else "pay off"
    return {"amount": amount, "yearly_return": yearly_return, "results": res, "loans": ls,
            "verdict": better,
            "notes": ["Wederbeleggingsvergoeding: maximaal 3 maanden interest op het afgeloste bedrag (hypothecair "
                      "krediet). Vraag je bank het exacte bedrag.",
                      "Krijg je voor deze lening nog een belastingvoordeel (woonbonus of langetermijnsparen bij "
                      "leningen van vóór 2020)? Dan verlies je dat deels bij vervroegd aflossen: dat zit hier niet in.",
                      "Beleggen heeft risico; aflossen geeft een zekere besparing tegen de rentevoet van de lening."]}
