"""r20: private and company apart."""
import os
import tempfile
from datetime import date

import pytest

from centen import analytics as A, db, demo, networth, planner, scopes

T = date(2026, 11, 13)


@pytest.fixture()
def conn():
    os.environ["CENTEN_HOME"] = tempfile.mkdtemp()
    c = db.connect(os.environ["CENTEN_HOME"] + "/t.db")
    demo.generate_full(c, T)
    return c


def test_split_adds_up(conn):
    s = scopes.split(conn, T)
    P, B = s["personal"], s["business"]
    assert P["net_worth"] == pytest.approx(P["available_now"] + P["locked"] + P["owned"], abs=0.05)
    assert P["available_now"] == pytest.approx(P["cash"] + P["investments"], abs=0.05)
    # the company: its cash minus what it still owes, and what that leaves you after withholding
    assert B["free_cash"] == pytest.approx(B["cash"] - B["owed_estimate"], abs=0.05)
    assert B["net_to_you"] == pytest.approx(max(0, B["net_worth"]) * (1 - B["payout_rate"]), abs=0.05)
    assert B["owed_estimate"] > 0
    # together: the combined net worth minus the tax still owed
    nw = A.position(conn, today=T)["net_worth"]["total"]
    assert s["total"] == pytest.approx(nw - B["owed_estimate"], abs=1)
    # the company's money is never in what you can spend
    assert P["cash"] == pytest.approx(A.totals(conn)["personal"], abs=0.05)


def test_vvprbis_rate(conn):
    from centen import remuneration as R
    R.set_profile(conn, vvprbis=True)
    assert scopes.split(conn, T)["business"]["payout_rate"] == 0.18
    assert planner.inputs(conn, T)["company_extraction_tax"] == 0.18


def test_no_company(tmp_path):
    os.environ["CENTEN_HOME"] = str(tmp_path)
    c = db.connect(str(tmp_path / "t.db"))
    demo.generate_full(c, T)
    c.execute("DELETE FROM accounts WHERE scope='business'")
    s = scopes.split(c, T)
    assert s["business"] is None and s["total"] == s["personal"]["net_worth"]


def test_history_split(conn):
    networth.reconstruct(conn, T)
    networth.snapshot(conn, T)
    pts = [p for p in networth.timeline(conn)["points"] if p["total"] is not None]
    assert pts and all(p["personal"] + p["business"] == pytest.approx(p["total"], abs=0.05) for p in pts)
    assert pts[-1]["business"] == pytest.approx(pts[-1]["cash_business"], abs=0.05)  # the demo BV holds only cash
