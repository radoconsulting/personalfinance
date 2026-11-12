from datetime import date

import pytest

from centen import analytics as A, dashboard, manual
from test_business import biz  # noqa: F401  (fixture)

TODAY = date(2026, 11, 6)


def _iban():
    from centen.money import is_valid_iban
    for n in range(1000):
        cand = f"6450000{n:05d}"
        chk = 98 - int(cand + "111400") % 97
        iban = f"BE{chk:02d}{cand}"
        if is_valid_iban(iban):
            return iban
    raise AssertionError


def _transfer(conn, account_id, amount, day, iban, name="Acme Consulting bv"):
    from centen import db
    db.insert_transactions(conn, [{
        "id": f"t-{account_id}-{day}-{amount}", "account_id": account_id, "booking_date": day, "value_date": day,
        "amount": amount, "currency": "EUR", "description": "Overschrijving naar spaarrekening", "remittance": "sparen",
        "counterparty_name": name, "counterparty_iban": iban, "status": "booked", "source": "test"}])
    conn.commit()


@pytest.fixture()
def biz_savings(biz):  # noqa: F811
    iban = _iban()
    bv = "bv"
    _transfer(biz, bv, -40000.0, "2026-09-02", iban)
    from centen.categorize import recategorize
    recategorize(biz)
    return biz, iban, bv


def test_suggest_then_add_by_last4(biz_savings):
    conn, iban, bv = biz_savings
    cand = [c for c in manual.suggestions(conn) if c["last4"] == iban[-4:]]
    assert cand and cand[0]["you_sent_to_it"] == 40000.0 and cand[0]["bank_guess"] == "Bank Van Breda"
    before = A.totals(conn)["business"]
    res = manual.add_account(conn, iban[-4:], 40000.0, "2026-09-02")
    assert res["scope"] == "business" and res["type"] == "savings" and res["estimated_balance"] == 40000.0
    assert res["name"] == "Acme Consulting BV"  # takes the linked account's spelling
    assert A.totals(conn)["business"] == pytest.approx(before + 40000.0)
    assert not [c for c in manual.suggestions(conn) if c["last4"] == iban[-4:]]


def test_estimate_follows_transfers_and_new_balance(biz_savings):
    conn, iban, bv = biz_savings
    manual.add_account(conn, iban, 40000.0, "2026-09-02", name="Spaar BV", bank="Bank Van Breda", scope="business")
    _transfer(conn, bv, -5000.0, "2026-10-01", iban)
    _transfer(conn, bv, 1200.0, "2026-10-20", iban)
    acc = [a for a in A.accounts_overview(conn) if a["name"] == "Spaar BV"][0]
    assert acc["estimated"] and acc["balance"] == 43800.0 and acc["transfers_since_entry"] == 2
    manual.set_balance(conn, "spaar bv", 44150.25, "2026-11-01")  # interest included by the bank
    acc = [a for a in A.accounts_overview(conn) if a["name"] == "Spaar BV"][0]
    assert acc["balance"] == 44150.25 and acc["transfers_since_entry"] == 0


def test_transfers_become_own_account_moves(biz_savings):
    conn, iban, bv = biz_savings
    manual.add_account(conn, iban[-4:], 40000.0, "2026-09-02")
    cat, internal = conn.execute("SELECT category, internal FROM transactions WHERE counterparty_iban=?", (iban,)).fetchone()
    assert cat == "Own accounts" and internal == "own"


def test_errors_and_remove(biz_savings):
    conn, iban, bv = biz_savings
    with pytest.raises(ValueError):
        manual.add_account(conn, "9999", 1.0)
    linked = conn.execute("SELECT iban FROM accounts WHERE id=?", (bv,)).fetchone()[0]
    with pytest.raises(ValueError):
        manual.add_account(conn, linked, 1.0)
    manual.add_account(conn, iban[-4:], 100.0)
    out = manual.remove(conn, iban[-4:])
    assert out["account"].endswith(iban[-4:])
    assert not conn.execute("SELECT 1 FROM accounts WHERE iban=?", (iban,)).fetchone()


def test_dashboard_marks_estimate(biz_savings):
    conn, iban, bv = biz_savings
    manual.add_account(conn, iban[-4:], 40000.0, "2026-09-02")
    d = dashboard.collect(conn, TODAY)
    est = [a for a in d["accounts"] if a.get("estimated")]
    assert len(est) == 1 and est[0]["balance"] == 40000.0 and est[0]["balance_entered"]["date"] == "2026-09-02"
