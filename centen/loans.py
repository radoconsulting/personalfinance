"""Loans with a repayment plan (e.g. a mortgage): from the terms on your loan statement centen keeps the amount still
owed up to date by itself, month by month, and splits every instalment into interest and capital.

Terms per loan account (an asset account of type 'loan'), in their own table `loan_terms`:
- principal, contract date, maturity date and the periodic (monthly) interest rate, as on the statement;
- an anchor: the amount still owed on a date (the statement) — the schedule runs forward and backward from it;
- the instalment: taken from the repayments seen on the bank account (recognised by the dossier number), else the
  annuity that pays the anchor balance off by the maturity date.
Instalments fall on the maturity's day of the month. The figures are estimates between statements: a new statement
(set_loan_terms with a new outstanding amount) resets the anchor.
"""
from __future__ import annotations

from datetime import date, timedelta
from statistics import median

SCHEMA = """
CREATE TABLE IF NOT EXISTS loan_terms (
    account_id         TEXT PRIMARY KEY,
    principal          REAL,                -- amount borrowed
    contract_date      TEXT,
    maturity_date      TEXT NOT NULL,       -- last instalment
    monthly_rate       REAL NOT NULL,       -- periodic rate per month, as a fraction (0,14 % -> 0.0014)
    anchor_date        TEXT NOT NULL,       -- date of the statement
    anchor_outstanding REAL NOT NULL,       -- amount still owed on anchor_date
    instalment         REAL,                -- fixed monthly instalment; NULL = from the bank payments / annuity
    updated_at         TEXT DEFAULT (datetime('now'))
);
"""

EPS = 0.005


def r2(x):
    return None if x is None else round(float(x), 2)


def ensure(conn) -> None:
    conn.executescript(SCHEMA)


def _d(s) -> date:
    return s if isinstance(s, date) else date.fromisoformat(str(s)[:10])


def _add_months(d: date, n: int, day: int) -> date:
    import calendar
    m = d.month - 1 + n
    y, m = d.year + m // 12, m % 12 + 1
    return date(y, m, min(day, calendar.monthrange(y, m)[1]))


def terms(conn, acc_id: str) -> dict | None:
    ensure(conn)
    r = conn.execute("SELECT * FROM loan_terms WHERE account_id=?", (acc_id,)).fetchone()
    return dict(r) if r else None


def annuity(balance: float, rate: float, n: int) -> float:
    if n <= 0:
        return balance
    return balance / n if rate == 0 else balance * rate / (1 - (1 + rate) ** -n)


def _due_dates(t: dict, start: date, end: date) -> list[date]:
    """Instalment dates (on the maturity's day of the month) in (start, end]."""
    mat = _d(t["maturity_date"])
    first = _d(t["contract_date"]) if t.get("contract_date") else date(1990, 1, 1)
    out, d = [], date(start.year, start.month, 1)
    d = _add_months(d, 0, mat.day)
    if d <= start:
        d = _add_months(d, 1, mat.day)
    while d <= min(end, mat):
        if d > first:
            out.append(d)
        d = _add_months(d, 1, mat.day)
    return out


def _payments_left(t: dict, after: date) -> int:
    return len(_due_dates(t, after, _d(t["maturity_date"])))


def instalment(conn, acc_id: str, t: dict | None = None) -> tuple[float, str]:
    """(monthly instalment, where it comes from)."""
    t = t or terms(conn, acc_id)
    if t.get("instalment"):
        return float(t["instalment"]), "entered"
    pays = [-r[0] for r in conn.execute(
        "SELECT amount FROM transactions WHERE asset_id=? AND status='booked' AND amount < 0 ORDER BY booking_date DESC "
        "LIMIT 6", (acc_id,))]
    if len(pays) >= 2:
        return round(median(pays), 2), "bank payments"
    n = _payments_left(t, _d(t["anchor_date"]))
    return round(annuity(t["anchor_outstanding"], t["monthly_rate"], n), 2), "annuity"


def balance_at(conn, acc_id: str, when: date, t: dict | None = None) -> float | None:
    """Amount still owed on `when` (after any instalment due that day)."""
    t = t or terms(conn, acc_id)
    if not t:
        return None
    when = _d(when)
    if t.get("contract_date") and when < _d(t["contract_date"]):
        return 0.0
    if when >= _d(t["maturity_date"]):
        return 0.0
    r, a = t["monthly_rate"], _d(t["anchor_date"])
    inst, _ = instalment(conn, acc_id, t)
    bal = t["anchor_outstanding"]
    if when >= a:
        for _ in _due_dates(t, a, when):
            bal = max(0.0, bal * (1 + r) - inst)
    else:
        for _ in reversed(_due_dates(t, when, a)):
            bal = (bal + inst) / (1 + r)
        if t.get("principal"):
            bal = min(bal, t["principal"])
    return r2(bal)


def schedule(conn, acc_id: str, start: date, months: int | None = None, t: dict | None = None) -> list[dict]:
    """Instalments from `start` on (all remaining when months is None): date, interest, capital, balance after."""
    t = t or terms(conn, acc_id)
    r = t["monthly_rate"]
    inst, _ = instalment(conn, acc_id, t)
    bal = balance_at(conn, acc_id, start, t) or 0.0
    out = []
    for d in _due_dates(t, start, _d(t["maturity_date"])):
        if bal <= EPS:
            break
        interest = bal * r
        pay = min(inst, bal + interest) if d < _d(t["maturity_date"]) else bal + interest
        cap = pay - interest
        bal = max(0.0, bal - cap)
        out.append({"date": d.isoformat(), "instalment": r2(pay), "interest": r2(interest), "capital": r2(cap),
                    "balance": r2(bal)})
        if months and len(out) >= months:
            break
    return out


def _year_split(conn, acc_id: str, t: dict, year: int, today: date) -> dict:
    """Interest and capital of the instalments due in `year` up to today."""
    r = t["monthly_rate"]
    inst, _ = instalment(conn, acc_id, t)
    start = date(year - 1, 12, 31)
    bal = balance_at(conn, acc_id, start, t) or 0.0
    interest = capital = 0.0
    for _ in _due_dates(t, start, min(today, date(year, 12, 31))):
        i = bal * r
        c = min(inst - i, bal)
        interest, capital, bal = interest + i, capital + c, bal - c
    return {"interest": r2(interest), "capital": r2(capital)}


def describe(conn, acc_id: str, today: date | None = None) -> dict | None:
    t = terms(conn, acc_id)
    if not t:
        return None
    today = today or date.today()
    inst, src = instalment(conn, acc_id, t)
    now = balance_at(conn, acc_id, today, t)
    rest = schedule(conn, acc_id, today, t=t)
    nxt = rest[0] if rest else None
    annual = (1 + t["monthly_rate"]) ** 12 - 1
    out = {
        "principal": r2(t["principal"]), "contract_date": t["contract_date"], "maturity_date": t["maturity_date"],
        "monthly_rate": t["monthly_rate"], "annual_rate_equivalent": round(annual, 5),
        "statement": {"date": t["anchor_date"], "outstanding": r2(t["anchor_outstanding"])},
        "outstanding_now": now, "instalment": r2(inst), "instalment_source": src,
        "repaid_capital": r2(t["principal"] - now) if t.get("principal") and now is not None else None,
        "repaid_share": round((t["principal"] - now) / t["principal"], 4) if t.get("principal") and now is not None
        else None,
        "payments_left": len(rest), "next": nxt,
        "remaining_interest": r2(sum(x["interest"] for x in rest)),
        "this_year": _year_split(conn, acc_id, t, today.year, today),
    }
    if nxt:
        out["next_split"] = {"interest": nxt["interest"], "capital": nxt["capital"]}
    return out


def set_terms(conn, account: str, outstanding: float, maturity_date: str, monthly_rate: float,
              principal: float | None = None, contract_date: str | None = None, statement_date: str | None = None,
              instalment_amount: float | None = None, today: date | None = None) -> dict:
    """Store (or replace) the terms of a loan account. monthly_rate as on the statement: 0,14 % -> 0.14 or 0.0014."""
    from . import assets
    ensure(conn)
    acc = assets.find(conn, account)
    if acc["account_type"] != "loan":
        raise ValueError(f"{acc['name']} is not a loan account (type {acc['account_type']}).")
    rate = float(monthly_rate)
    if rate >= 0.05:          # given in percent
        rate = rate / 100.0
    if not 0 <= rate < 0.05:
        raise ValueError("monthly_rate looks wrong: give the periodic rate per month, e.g. 0.14 (%).")
    today = today or date.today()
    sd = (statement_date or today.isoformat())[:10]
    _d(maturity_date), _d(sd)
    if contract_date:
        _d(contract_date)
    conn.execute("INSERT OR REPLACE INTO loan_terms(account_id, principal, contract_date, maturity_date, monthly_rate, "
                 "anchor_date, anchor_outstanding, instalment, updated_at) VALUES (?,?,?,?,?,?,?,?,datetime('now'))",
                 (acc["id"], principal, contract_date, maturity_date[:10], rate, sd, float(outstanding),
                  instalment_amount))
    # the statement is also the loan's value entry, so everything that reads loan values stays consistent
    assets._set(conn, acc["id"], assets.VALUATION, abs(float(outstanding)), sd, today)
    conn.commit()
    return {"account": acc["name"], **(describe(conn, acc["id"], today) or {})}


def all_loans(conn, today: date | None = None) -> list[dict]:
    ensure(conn)
    out = []
    for r in conn.execute("SELECT a.id, a.name, a.bank, a.scope FROM accounts a JOIN loan_terms t ON t.account_id=a.id "
                          "ORDER BY a.name"):
        d = describe(conn, r["id"], today)
        out.append({"account": r["name"], "provider": r["bank"], "scope": r["scope"], **d})
    return out


def overview(conn, today: date | None = None) -> dict:
    """Property and loans together: values, what's still owed, equity, this year's interest and capital, and the
    balance per month until the last loan is paid off."""
    from . import assets
    today = today or date.today()
    ls = all_loans(conn, today)
    props = [a for a in assets.list_accounts(conn, today) if a["type"] == "property"]
    owed = sum(x["outstanding_now"] or 0 for x in ls)
    value = sum(a.get("value") or 0 for a in props)
    curve = {}
    if ls:
        ids = {r[1]: r[0] for r in conn.execute("SELECT a.id, a.name FROM accounts a JOIN loan_terms t ON "
                                                "t.account_id=a.id")}
        end = max(_d(x["maturity_date"]) for x in ls)
        d = date(today.year, today.month, 1)
        while d <= _add_months(end, 1, 1):
            m_end = _add_months(d, 1, 1) - timedelta(days=1)
            curve[m_end.isoformat()[:7]] = r2(sum(balance_at(conn, ids[x["account"]], m_end) or 0 for x in ls))
            d = _add_months(d, 1, 1)
    return {
        "property": [{k: a.get(k) for k in ("name", "value", "value_entered", "stale", "scope")} for a in props],
        "loans": ls, "property_value": r2(value), "owed": r2(owed), "equity": r2(value - owed) if props else None,
        "loan_to_value": round(owed / value, 4) if value else None,
        "monthly_instalments": r2(sum(x["instalment"] or 0 for x in ls)),
        "this_year": {"interest": r2(sum(x["this_year"]["interest"] or 0 for x in ls)),
                      "capital": r2(sum(x["this_year"]["capital"] or 0 for x in ls))},
        "remaining_interest": r2(sum(x["remaining_interest"] or 0 for x in ls)),
        "debt_free_on": max((x["maturity_date"] for x in ls), default=None),
        "curve": [{"month": k, "owed": v} for k, v in curve.items()],
        "note": "Openstaand saldo geschat uit je laatste leningsoverzicht, de rentevoet en de afbetalingen; "
                "de woningwaarde is jouw schatting.",
    }


def new_statement(conn, account: str, outstanding: float, statement_date: str | None = None,
                  today: date | None = None) -> dict:
    """A newer loan statement (or an extra repayment): reset the amount owed on that date, keep the other terms."""
    from . import assets
    acc = assets.find(conn, account)
    t = terms(conn, acc["id"])
    if not t:
        raise ValueError(f"{acc['name']} has no repayment plan yet: set its terms first (set_loan_terms).")
    return set_terms(conn, acc["name"], outstanding, t["maturity_date"], t["monthly_rate"], t["principal"],
                     t["contract_date"], statement_date, t["instalment"], today)
