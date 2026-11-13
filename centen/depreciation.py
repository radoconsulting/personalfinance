"""Things that lose value over time (r18): a car, a boat, equipment. They count in your net worth at an estimated
value that falls by a fixed percentage per year (declining balance) from the purchase price, so a car bought with cash
moves money from "cash" to "car" instead of disappearing from your net worth.

The schedule writes month-end valuations (and today's) for the account, so the net worth history and every view
that reads valuations pick it up. When you enter a real value later (a quote, a sale offer), the schedule continues
from that value and date. The purchase payment itself stays an expense in your spending.
"""
from __future__ import annotations

from datetime import date, timedelta

from . import analytics as A
from . import assets as AS
from . import db

SCHEMA = """
CREATE TABLE IF NOT EXISTS depreciation (
    account_id      TEXT PRIMARY KEY,
    purchase_price  REAL NOT NULL,
    purchase_date   TEXT NOT NULL,
    rate            REAL NOT NULL,          -- per year, 0.10 = 10 %
    floor_pct       REAL DEFAULT 0,         -- never below this share of the purchase price
    anchor_value    REAL NOT NULL,          -- last known value (purchase or entered)
    anchor_date     TEXT NOT NULL,
    updated_at      TEXT DEFAULT (datetime('now'))
);
"""


def r2(x):
    return None if x is None else round(float(x), 2)


def ensure(conn) -> None:
    conn.executescript(SCHEMA)


def _rate(v: float) -> float:
    v = float(v)
    v = v / 100 if v >= 1 else v
    if not 0 <= v < 0.9:
        raise ValueError("yearly depreciation is a percentage between 0 and 90 (e.g. 8)")
    return v


def value_on(row, d: date) -> float:
    a = date.fromisoformat(row["anchor_date"])
    years = max(0.0, (d - a).days / 365.25)
    v = row["anchor_value"] * (1 - row["rate"]) ** years
    return max(v, row["purchase_price"] * (row["floor_pct"] or 0))


def _month_ends(start: date, end: date):
    d = A.add_months(start.replace(day=1), 1) - timedelta(days=1)
    while d <= end:
        yield d
        d = A.add_months(d.replace(day=1), 2) - timedelta(days=1)


def apply(conn, today: date | None = None) -> int:
    """(Re)write the scheduled valuations up to today. Returns the number of accounts updated."""
    today = today or date.today()
    ensure(conn)
    n = 0
    for row in conn.execute("SELECT * FROM depreciation").fetchall():
        a = date.fromisoformat(row["anchor_date"])
        conn.execute("DELETE FROM balances WHERE account_id=? AND balance_type='valuation' AND as_of>?",
                     (row["account_id"], row["anchor_date"]))
        for d in list(_month_ends(a, today)) + ([today] if today > a else []):
            if d > a:
                db.set_balance(conn, row["account_id"], d.isoformat(), "valuation", round(value_on(row, d), 2))
        n += 1
    conn.commit()
    return n


def set_schedule(conn, account: str, purchase_price: float | None = None, purchase_date: str | None = None,
                 yearly_rate: float | None = None, floor_pct: float | None = None, today: date | None = None) -> dict:
    ensure(conn)
    acc = AS.find(conn, account)
    if acc["account_type"] in ("loan", "investment", "crypto", "pension"):
        raise ValueError("Depreciation is for things like a car, not for investments, pensions or loans.")
    cur = conn.execute("SELECT * FROM depreciation WHERE account_id=?", (acc["id"],)).fetchone()
    if cur is None and (purchase_price is None or purchase_date is None or yearly_rate is None):
        raise ValueError("Give purchase_price, purchase_date and the yearly depreciation the first time.")
    price = float(purchase_price) if purchase_price is not None else cur["purchase_price"]
    pdate = (date.fromisoformat(purchase_date[:10]).isoformat() if purchase_date else cur["purchase_date"])
    rate = _rate(yearly_rate) if yearly_rate is not None else cur["rate"]
    fl = (float(floor_pct) / 100 if floor_pct is not None and floor_pct > 1 else floor_pct)
    fl = fl if fl is not None else (cur["floor_pct"] if cur else 0.0)
    anchor_v, anchor_d = (price, pdate) if cur is None or purchase_price is not None or purchase_date else \
        (cur["anchor_value"], cur["anchor_date"])
    conn.execute("INSERT INTO depreciation(account_id, purchase_price, purchase_date, rate, floor_pct, anchor_value, "
                 "anchor_date) VALUES (?,?,?,?,?,?,?) ON CONFLICT(account_id) DO UPDATE SET "
                 "purchase_price=excluded.purchase_price, purchase_date=excluded.purchase_date, rate=excluded.rate, "
                 "floor_pct=excluded.floor_pct, anchor_value=excluded.anchor_value, anchor_date=excluded.anchor_date, "
                 "updated_at=datetime('now')", (acc["id"], price, pdate, rate, fl, anchor_v, anchor_d))
    db.set_balance(conn, acc["id"], anchor_d, "valuation", round(anchor_v, 2))
    apply(conn, today)
    return describe(conn, acc["id"], today)


def reanchor(conn, acc_id: str, value: float, when: str) -> None:
    """A value the user entered: the schedule continues from there."""
    ensure(conn)
    if conn.execute("SELECT 1 FROM depreciation WHERE account_id=?", (acc_id,)).fetchone():
        conn.execute("UPDATE depreciation SET anchor_value=?, anchor_date=?, updated_at=datetime('now') "
                     "WHERE account_id=?", (float(value), when[:10], acc_id))


def remove_schedule(conn, account: str) -> dict:
    ensure(conn)
    acc = AS.find(conn, account)
    conn.execute("DELETE FROM depreciation WHERE account_id=?", (acc["id"],))
    conn.commit()
    return {"removed_schedule": acc["name"], "note": "The values written so far stay; enter new values by hand."}


def describe(conn, acc_id: str, today: date | None = None) -> dict:
    today = today or date.today()
    row = conn.execute("SELECT d.*, a.name FROM depreciation d JOIN accounts a ON a.id=d.account_id "
                       "WHERE d.account_id=?", (acc_id,)).fetchone()
    if row is None:
        return {}
    now = value_on(row, today)
    nxt = value_on(row, A.add_months(today, 12))
    return {"name": row["name"], "purchase_price": r2(row["purchase_price"]), "purchase_date": row["purchase_date"],
            "yearly_rate": row["rate"], "floor_pct": row["floor_pct"], "value_now": r2(now),
            "lost_so_far": r2(row["purchase_price"] - now), "value_in_12_months": r2(nxt),
            "cost_per_month_next_year": r2((now - nxt) / 12),
            "based_on": {"value": r2(row["anchor_value"]), "date": row["anchor_date"],
                         "kind": "purchase" if row["anchor_date"] == row["purchase_date"] and
                         abs(row["anchor_value"] - row["purchase_price"]) < 0.01 else "entered value"}}


def list_all(conn, today: date | None = None) -> list[dict]:
    ensure(conn)
    return [describe(conn, r[0], today) for r in conn.execute("SELECT account_id FROM depreciation")]


def add_vehicle(conn, name: str, purchase_price: float, purchase_date: str, yearly_rate: float,
                scope: str = "personal", provider: str | None = None, floor_pct: float | None = None,
                today: date | None = None) -> dict:
    """Add a car (or another depreciating thing) as an asset with a depreciation schedule."""
    AS.add_account(conn, name, "asset", value=purchase_price, value_date=purchase_date, provider=provider,
                   scope=scope, today=today)
    return set_schedule(conn, name, purchase_price, purchase_date, yearly_rate, floor_pct, today)
