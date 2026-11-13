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


NN_CERT = """Uitgiftedatum: 10.09.2024
Een te verwachten kapitaal op pensioenleeftijd.
Premies (EUR)(1)
Eenmalige premie op 01.10.2021
• Pensioen/overlijden: 5.945,00
Totaal: 6.688,64
Jaarlijkse premies van 01.10.2022 tot 01.10.2023
• Pensioen/overlijden: 4.500,00
• Arbeidsongeschiktheid: 741,22
Totaal: 5.359,07
Jaarlijkse premies van 01.10.2024 tot 01.10.2053
• Pensioen/overlijden: 4.890,00
• Premievrijstelling: 126,86
Totaal: 5.758,10
Verwachte reserves op 31.01.2055 (67 jaar): 215.607,14 959.662,61 1.345.344,86
conversiecoëfficiënt op basis van: eindleeftijd van 65 jaar
Detail reserves op 10.09.2024
Verworven reserves winstdeling inbegrepen op 10.09.2024
Overeenkomst Bron Beleggingsvorm EUR
310-1 Premie Fonds A 26.634,80(1)
Fonds B 27.971,16(2)
Subtotaal: 54.605,96
310-2 Premie Fonds A 17.597,04(3)
Subtotaal: 17.597,04
Totaal: 72.203,00
"""


def test_nn_certificate():
    from centen import pension
    f = pension.parse_statement_text(NN_CERT)
    assert f["reserve"]["amount"] == 72203.0 and f["reserve"]["date"] == "2024-09-10"
    assert f["expected"]["amount"] == 959662.61
    assert f["premium"]["amount"] == 4890.0
    assert f["age"] == 67


def test_mypension_certificate_and_reminder(conn):
    from centen import notify, pension
    txt = NN_CERT.replace("10.09.2024", "04.04.2026").replace("72.203,00", "93.438,85").replace(
        "215.607,14 959.662,61 1.345.344,86", "227.591,11 983.420,30 1.363.581,94")
    f = pension.parse_statement_text("pagina 1 van 13\nReden van uitgifte: Sigedis\n" + txt)
    assert f["reserve"]["amount"] == 93438.85 and f["reserve"]["date"] == "2026-04-04"
    assert f["expected"]["amount"] == 983420.3 and f["premium"]["amount"] == 4890.0
    # yearly reminder from mid-September when the pension value is older than half a year
    kinds = lambda d: [i for i in notify.collect(conn, d) if i["kind"] == "pension_fiche"]  # noqa: E731
    assert not kinds(date(2026, 9, 1))
    r = conn.execute("SELECT a.id FROM accounts a WHERE a.account_type='pension'").fetchone()
    conn.execute("DELETE FROM balances WHERE account_id=? AND balance_type='valuation'", (r[0],))
    conn.execute("INSERT INTO balances(account_id, as_of, balance_type, amount) VALUES (?,?,?,?)",
                 (r[0], "2026-01-01", "valuation", 40000))
    assert kinds(date(2026, 9, 20))
    conn.execute("UPDATE balances SET as_of='2026-09-10' WHERE account_id=? AND balance_type='valuation'", (r[0],))
    assert not kinds(date(2026, 9, 20))
