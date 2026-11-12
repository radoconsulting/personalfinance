import sqlite3

import pytest

from centen import analytics as A
from centen import db

from datetime import date

TODAY = date(2026, 11, 6)


def test_recurring_detects_bills_and_price_changes(demo_conn):
    rec = {r["merchant"]: r for r in A.recurring(demo_conn, TODAY)}
    for m in ("Netflix", "Spotify", "Engie", "Telenet", "Proximus", "De Watergroep", "Basic-Fit", "AG Insurance"):
        assert m in rec, m
    assert rec["De Watergroep"]["cadence"] == "quarterly"
    assert rec["Engie"]["paid_by"] == "direct debit"
    assert rec["Netflix"]["price_change"]["from"] == 13.49 and rec["Netflix"]["price_change"]["to"] == 15.99
    assert "Colruyt" not in rec  # weekly groceries are habits, not bills
    salary = [r for r in rec.values() if r["category"] == "Salary"][0]
    assert salary["direction"] == "in" and salary["cadence"] == "monthly"


def test_upcoming_bills(demo_conn):
    up = A.upcoming(demo_conn, 30, TODAY)
    merchants = [i["merchant"] for i in up["items"]]
    assert "Netflix" in merchants and "Engie" in merchants
    assert all(i["date"] >= TODAY.isoformat() for i in up["items"])
    assert "DEGIRO" not in merchants  # transfers are excluded by default
    assert "DEGIRO" in [i["merchant"] for i in A.upcoming(demo_conn, 30, TODAY, include_transfers=True)["items"]]


def test_unusual_activity(demo_conn):
    types = {(a["type"], a["merchant"].split()[0]) for a in A.unusual(demo_conn, 60, TODAY)}
    assert ("possible_duplicate", "Pizzeria") in types
    assert ("large_new_merchant", "Coolblue") in types
    assert ("price_increase", "Netflix") in types and ("price_increase", "Engie") in types
    assert ("fee", "Debetrente") in types
    assert not any(t == "fee" and m == "Beheerskosten" for t, m in types)  # regular package fee is expected


def test_until_payday(demo_conn):
    p = A.until_payday(demo_conn, TODAY)
    assert "2026-11-23" <= p["next_payday"] <= "2026-11-25"  # salary lands on the 25th or the working day before
    assert p["available_now"] > 0 and p["bills_before_payday"] > 0
    assert p["left_after_bills"] == pytest.approx(p["available_now"] - p["bills_before_payday"]
                                                  - p["planned_transfers_before_payday"], abs=0.02)
    assert p["excluded_one_offs_from_daily_average"][0]["merchant"] == "Coolblue"
    assert A.until_payday(demo_conn, TODAY, payday_day=1)["next_payday"] == "2026-12-01"


def test_breakdown_excludes_transfers(demo_conn):
    b = A.breakdown(demo_conn, TODAY.replace(month=10, day=1), TODAY.replace(month=10, day=31))
    keys = {i["key"] for i in b["items"]}
    assert "Own accounts" not in keys and "Savings & investments" not in keys and "Credit card bill" not in keys
    assert "Subscriptions & streaming" in keys  # card purchases are counted on the card account
    assert b["total"] == pytest.approx(sum(i["amount"] for i in b["items"]), abs=0.05)


def test_cashflow_and_overview(demo_conn):
    cf = A.cashflow(demo_conn, 6, TODAY)
    assert len(cf) == 6 and cf[-1]["partial"] and cf[0]["income"] > 3000
    ov = A.overview(demo_conn, TODAY)
    assert ov["balances"]["savings"] > 0 and ov["this_month"]["spending"] > 0
    assert ov["upcoming_14_days"]["count"] >= 3


def test_search(demo_conn):
    r = A.search(demo_conn, query="netflix")
    assert r["matches"] >= 12 and all("iban" not in str(t.keys()) for t in r["transactions"])
    r = A.search(demo_conn, category="Groceries", account="zicht", date_from="2026-10-01", date_to="2026-10-31")
    assert r["matches"] > 3 and r["sum"] < 0


def test_readonly_sql_hides_sensitive_columns(demo_conn, tmp_path):
    path = demo_conn.execute("PRAGMA database_list").fetchone()[2]
    ro = db.connect_readonly(path)
    row = ro.execute("SELECT iban, iban_masked FROM accounts WHERE iban_masked IS NOT NULL").fetchone()
    assert row[0] is None and row[1].startswith("BE…")
    assert ro.execute("SELECT COUNT(*) FROM transactions WHERE counterparty_iban IS NOT NULL").fetchone()[0] == 0
    for bad in ("DELETE FROM transactions", "SELECT * FROM bank_links", "PRAGMA table_info(accounts)",
                "ATTACH DATABASE '/tmp/x.db' AS x", "UPDATE accounts SET name='x'"):
        with pytest.raises(sqlite3.DatabaseError):
            ro.execute(bad)
    assert ro.execute("SELECT category, ROUND(SUM(-amount),2) FROM transactions WHERE kind='expense' "
                      "GROUP BY category ORDER BY 2 DESC LIMIT 1").fetchone()[0] == "Loans & mortgage"
