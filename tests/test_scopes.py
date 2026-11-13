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


def test_fiche_return_used(conn):
    from centen import pension
    txt = NN_CERT + """(3) Tweede projectie berekend op basis van de volgende hypothesen:
• jaarlijks rendement van 'Fonds A': 6,60%;
• jaarlijks rendement van 'Fonds B': 6,70%.
(4) Derde projectie berekend op basis van de volgende hypothesen:
• jaarlijks rendement van 'Fonds A': 8,00%;
"""
    f = pension.parse_statement_text(txt)
    assert f["expected"]["return"] == pytest.approx(0.0665)
    pension.set_profile(conn, birth_date="1988-01-28")
    acc = pension.overview(conn, T)["pillars"][1]["accounts"][0]["name"]
    pension.import_statement(conn, text=txt, account=acc, apply=True, today=T)
    a = pension.overview(conn, T)["pillars"][1]["accounts"][0]
    assert a["return_assumed"] == pytest.approx(0.0665) and a["return_from_fiche"]
    assert a["expected_at_retirement"] == 959662.61


# --- r24: the total cut into day-to-day / investments and wealth / pension ----------------------------------------

def test_pillars_add_up_to_the_total(conn):
    s = scopes.split(conn, T)
    pl, P, B = s["pillars"], s["personal"], s["business"]
    assert pl["daily"]["value"] + pl["wealth"]["value"] + pl["pension"]["value"] == pytest.approx(s["total"], abs=0.05)
    # day-to-day: the bank accounts, the company's after the tax it still owes
    assert pl["daily"]["value"] == pytest.approx(P["cash"] + B["free_cash"], abs=0.05)
    assert pl["daily"]["owed_business"] == pytest.approx(-B["owed_estimate"], abs=0.05)
    assert pl["pension"]["value"] == pytest.approx(P["pension"], abs=0.05)
    assert pl["wealth"]["owned_net"] == pytest.approx(pl["wealth"]["property"] + pl["wealth"]["possessions"]
                                                      + pl["wealth"]["loans"], abs=0.05)


def test_pillars_without_company(tmp_path):
    os.environ["CENTEN_HOME"] = str(tmp_path)
    c = db.connect(str(tmp_path / "t.db"))
    demo.generate_full(c, T)
    c.execute("DELETE FROM accounts WHERE scope='business'")
    s = scopes.split(c, T)
    pl = s["pillars"]
    assert pl["daily"]["cash_business"] is None and pl["daily"]["owed_business"] is None
    assert sum(pl[k]["value"] for k in ("daily", "wealth", "pension")) == pytest.approx(s["total"], abs=0.05)


def test_history_has_the_pension_part(conn):
    networth.reconstruct(conn, T)
    networth.snapshot(conn, T)
    pts = networth.timeline(conn)["points"]
    assert pts and all("pension" in p for p in pts)
    live = [p for p in pts if p["source"] == "live"][-1]
    pens = A.position(conn, today=T)["investments"]["by_type"].get("pension") or 0
    assert live["pension"] == pytest.approx(pens, abs=0.05)
    assert pens > 0  # the demo has a pension contract
    assert all(p["pension"] is None or 0 <= p["pension"] <= (p["investments"] or 0) + 0.05 for p in pts)


def test_dashboard_snapshot_keeps_the_pension_part(conn, tmp_path):
    from centen import dashboard
    networth.reconstruct(conn, T)
    data = dashboard.collect(conn, T)
    pos = {"net_worth": data["net_worth"], "cash": data["periods"][data["meta"]["default_period"]]["cash"],
           "investments": {"by_type": data["investments"]["totals"]["by_type"]}}
    networth.snapshot(conn, T, pos)
    live = [p for p in networth.timeline(conn)["points"] if p["source"] == "live"][-1]
    assert live["pension"] == pytest.approx(data["investments"]["totals"]["by_type"]["pension"], abs=0.05)
    # without the investments the pension part is unknown, not zero
    networth.snapshot(conn, T, {"net_worth": data["net_worth"], "cash": pos["cash"]})
    assert [p for p in networth.timeline(conn)["points"] if p["source"] == "live"][-1]["pension"] is None
