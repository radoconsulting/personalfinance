"""r18: salary or dividend, depreciating assets, paying off loans early, pension fiche, pension tab in the update app."""
import json
import shutil
import subprocess
from datetime import date

import pytest

from centen import app, assets, dashboard, db, depreciation, loans, pension, prepay, remuneration as R

TODAY = date(2026, 11, 13)


# --- salary or dividend -----------------------------------------------------------------------------------

def test_income_tax_and_social_contributions():
    assert R.income_tax(11550, 0, 0) == 0
    # 30.000 taxable: 16.720×25 % + 12.790×40 % + 490×45 % − 11.550×25 %
    assert R.income_tax(30000, 0, 0) == pytest.approx(4180 + 5116 + 220.5 - 2887.5, abs=0.01)
    assert R.income_tax(30000, 0, 0.075) == pytest.approx((4180 + 5116 + 220.5 - 2887.5) * 1.075, abs=0.01)
    assert R.income_tax(30000, 1, 0) < R.income_tax(30000, 0, 0)
    assert R.social_contributions(10000, 0) == pytest.approx(17374.08 * 0.205, abs=0.01)  # minimum
    assert R.social_contributions(200000, 0) == pytest.approx(75024.54 * 0.205 + (110562.42 - 75024.54) * 0.1416, abs=0.01)
    n = R.remuneration_net(50000, 0, 0.07)
    assert n["social"] == pytest.approx(R.social_contributions(n["taxable"], 0.0305), abs=0.05)
    assert n["net"] == pytest.approx(50000 - n["social"] - n["tax"], abs=0.01)
    assert 0.5 < n["net"] / 50000 < 0.7


def test_corporate_tax_needs_minimum_remuneration_for_reduced_rate():
    assert R.corporate_tax(100000, 50000) == pytest.approx(20000)
    assert R.corporate_tax(100000, 49999) == pytest.approx(25000)
    assert R.corporate_tax(150000, 60000) == pytest.approx(20000 + 12500)
    d = R.distribution(10000, False)
    assert d["ordinary"] == 7000 and "vvprbis" not in d
    assert d["liquidation_reserve"] == pytest.approx(10000 / 1.1 * (1 - 0.098), abs=0.01)
    assert R.distribution(10000, True)["vvprbis"] == 8200


def test_compare_finds_best_split(demo_conn):
    r = R.compare(demo_conn, TODAY, profit=150000, remuneration=30000)
    nets = [g["net_total"] for g in r["grid"]]
    assert r["best"]["net_total"] == max(nets)
    assert r["gain_vs_current"] >= 0
    at50 = next(g for g in r["grid"] if g["remuneration"] == 50000)
    below = next(g for g in r["grid"] if g["remuneration"] == 45000)
    assert at50["reduced_rate"] and not below["reduced_rate"]
    assert at50["net_total"] > below["net_total"]  # the reduced rate is worth reaching
    R.set_profile(demo_conn, municipal_rate=7.5, children=1, vvprbis=True)
    p = R.profile(demo_conn)
    assert p["municipal_rate"] == 0.075 and p["children"] == 1
    r2 = R.compare(demo_conn, TODAY, profit=150000, remuneration=30000)
    assert any(g["best_distribution"] == "vvprbis" for g in r2["grid"]) or \
        all(g["distribution_net"]["vvprbis"] <= g["distribution_net"][g["best_distribution"]] for g in r2["grid"])
    with pytest.raises(ValueError):
        R.set_profile(demo_conn, municipal_rate=30)


# --- depreciation -------------------------------------------------------------------------------------------

def test_vehicle_depreciates_and_reanchors(demo_conn):
    v = depreciation.add_vehicle(demo_conn, "Spyder", 139000, "2026-07-16", 5, today=TODAY)
    years = (TODAY - date(2026, 7, 16)).days / 365.25
    assert v["value_now"] == pytest.approx(139000 * 0.95 ** years, abs=0.5)
    acc = assets.find(demo_conn, "Spyder")
    rows = demo_conn.execute("SELECT as_of, amount FROM balances WHERE account_id=? AND balance_type='valuation' "
                             "ORDER BY as_of", (acc["id"],)).fetchall()
    assert [r[0] for r in rows] == ["2026-07-16", "2026-07-31", "2026-08-31", "2026-09-30", "2026-10-31", "2026-11-13"]
    assert all(a[1] > b[1] for a, b in zip(rows, rows[1:]))
    listed = [a for a in assets.list_accounts(demo_conn, TODAY) if a["name"] == "Spyder"][0]
    assert listed["value"] == pytest.approx(v["value_now"], abs=0.01)
    # the payment for the car is not linked to it (it stays an expense)
    assert listed["recognised_by"] in ([], None) or not listed.get("payments")
    assets.set_value(demo_conn, "Spyder", 145000, "2026-10-01", today=TODAY)
    depreciation.apply(demo_conn, TODAY)
    d = depreciation.list_all(demo_conn, TODAY)[0]
    assert d["based_on"] == {"value": 145000.0, "date": "2026-10-01", "kind": "entered value"}
    assert d["value_now"] == pytest.approx(145000 * 0.95 ** ((TODAY - date(2026, 10, 1)).days / 365.25), abs=0.5)
    depreciation.set_schedule(demo_conn, "Spyder", yearly_rate=10, today=TODAY)
    assert depreciation.list_all(demo_conn, TODAY)[0]["yearly_rate"] == 0.10
    with pytest.raises(ValueError):
        depreciation.set_schedule(demo_conn, "Spyder", yearly_rate=95, today=TODAY)
    depreciation.remove_schedule(demo_conn, "Spyder")
    assert depreciation.list_all(demo_conn, TODAY) == []


# --- paying off early ---------------------------------------------------------------------------------------

def test_prepayment_break_even_is_about_the_loan_rate():
    c = prepay.calc(54088.71, 0.0016, 595.33, 98, 20000, 0.05)
    assert c["reinvestment_fee"] == pytest.approx(20000 - 20000 / (1 + 3 * 0.0016), abs=0.01)
    assert c["shorter"]["months_earlier"] > 0 and c["lower"]["instalment"] < 595.33
    assert c["shorter"]["interest_saved"] > 0 and c["lower"]["interest_saved"] > 0
    assert c["shorter"]["advantage_vs_investing"] < 0  # 5 % beats a 1,9 % loan
    be = c["shorter"]["break_even_return"]
    assert c["loan_rate_yearly"] * 0.8 < be < c["loan_rate_yearly"] * 1.3
    low_ret = prepay.calc(54088.71, 0.0016, 595.33, 98, 20000, 0.0)
    assert low_ret["shorter"]["advantage_vs_investing"] > 0
    whole = prepay.calc(10000, 0.003, 500, 22, 50000, 0.05)
    assert whole["outstanding_after"] == 0 and whole["shorter"]["months_left"] == 0


def test_prepayment_js_matches_python():
    if not shutil.which("node"):
        pytest.skip("node not installed")
    tpl = dashboard.TEMPLATE.read_text(encoding="utf-8")
    js = tpl[tpl.index("function ppAnnuity"):tpl.index("function prepayCard")]
    for args in [(54088.71, 0.0016, 595.33, 98, 20000, 0.05), (28930.73, 0.0014, 743.61, 40, 5000, 0.02),
                 (10000, 0.003, 500, 22, 50000, 0.05)]:
        p = prepay.calc(*args)
        L = {"outstanding": args[0], "monthly_rate": args[1], "instalment": args[2], "payments_left": args[3]}
        code = js + f"\nconst r=ppCalc({json.dumps(L)},{args[4]},{args[5]},0.10,3);console.log(JSON.stringify(r))"
        r = json.loads(subprocess.run(["node", "-e", code], capture_output=True, text=True, check=True).stdout)
        assert r["shorter"]["adv"] == pytest.approx(p["shorter"]["advantage_vs_investing"], abs=0.05)
        assert r["lower"]["adv"] == pytest.approx(p["lower"]["advantage_vs_investing"], abs=0.05)
        assert r["shorter"]["months_left"] == p["shorter"]["months_left"]
        assert (r["shorter"]["be"] or 0) == pytest.approx(p["shorter"]["break_even_return"] or 0, abs=0.0002)


def test_prepayment_on_real_loans(demo_conn):
    assets.add_account(demo_conn, "Woonkrediet", "loan", value=50000, value_date="2026-10-01", today=TODAY)
    loans.set_terms(demo_conn, "Woonkrediet", 50000, "2034-12-18", 0.0016, statement_date="2026-10-01",
                    instalment_amount=595.33)
    r = prepay.analyse(demo_conn, 10000, None, 5, today=TODAY)
    assert r["results"][0]["loan"] == "Woonkrediet" and r["yearly_return"] == 0.05 and r["verdict"] == "invest"
    with pytest.raises(ValueError):
        prepay.analyse(demo_conn, 10000, "nope", today=TODAY)


# --- pension fiche ----------------------------------------------------------------------------------------

FICHE = """NN Insurance Belgium
Pensioenfiche IPT - situatie op 01/01/2026
Contract 123456 Scala Privilege
Verworven reserve op 01/01/2026: 101.234,56 EUR
Totale premie 2025 5.760,88 EUR
Te verwachten prestatie op de pensioendatum (leeftijd van 65 jaar)
412 345,00 €
"""


def test_parse_pension_fiche():
    f = pension.parse_statement_text(FICHE)
    assert f["reserve"]["amount"] == 101234.56 and f["reserve"]["date"] == "2026-01-01"
    assert f["expected"]["amount"] == 412345.0 and f["premium"]["amount"] == 5760.88
    assert f["age"] == 65 and f["statement_date"] == "2026-01-01"
    fr = pension.parse_statement_text("Réserves acquises au 01/01/2026 : 12.345,67 €\nPrime annuelle 1.200,00 €")
    assert fr["reserve"]["amount"] == 12345.67 and fr["premium"]["amount"] == 1200.0
    assert pension.parse_statement_text("niets bruikbaars hier") == {}


def test_import_pension_fiche_pdf_and_apply(demo_conn, tmp_path):
    reportlab = pytest.importorskip("reportlab")
    pytest.importorskip("pdfplumber")
    from reportlab.pdfgen import canvas
    pdf = tmp_path / "fiche.pdf"
    cv = canvas.Canvas(str(pdf))
    y = 800
    for line in FICHE.splitlines():
        cv.drawString(40, y, line.replace("€", "EUR"))
        y -= 18
    cv.save()
    assets.add_account(demo_conn, "IPT NN", "pension", value=90000, value_date="2025-06-01", today=TODAY)
    prev = pension.import_statement(demo_conn, str(pdf), today=TODAY)
    assert prev["account"] == "IPT NN" and not prev["applied"] and prev["found"]["reserve"]["amount"] == 101234.56
    done = pension.import_statement(demo_conn, str(pdf), "IPT NN", apply=True, today=TODAY)
    assert done["applied"]
    acc = [a for a in assets.list_accounts(demo_conn, TODAY) if a["name"] == "IPT NN"][0]
    assert acc["value_entered"]["amount"] == 101234.56 and acc["value_entered"]["date"] == "2026-01-01"
    s = pension.profile(demo_conn)["accounts"]["IPT NN"]
    assert s["expected_at_retirement"] == 412345.0 and s["premium_yearly"] == 5760.88
    assert reportlab


# --- update app: pension tab --------------------------------------------------------------------------------

def test_update_app_pension_actions(demo_conn):
    assets.add_account(demo_conn, "IPT NN", "pension", value=90000, value_date="2026-06-01", today=TODAY)
    acc = demo_conn.execute("SELECT id FROM accounts WHERE account_type='current'").fetchone()[0]
    db.insert_transactions(demo_conn, [dict(id="car", account_id=acc, booking_date="2026-07-16", amount=-139000.0,
                                            status="booked", counterparty_name="Porsche", merchant="Porsche",
                                            merchant_key="porsche", category="Car & maintenance", kind="expense",
                                            source="t")])
    st = app.state(demo_conn, TODAY)["pension"]
    assert st["pension_accounts"] == ["IPT NN"]
    car = next(x for x in st["big_expenses"] if x["id"] == "car")
    assert car["state"] == "auto"
    app.act(demo_conn, "pension_profile", {"birth_date": "1988-01-28", "stop_work_age": "58",
                                           "legal_pension_monthly": "1.881,92", "spending_monthly": "",
                                           "investing_yearly": "30.000"})
    p = pension.profile(demo_conn)
    assert p["birth_date"] == "1988-01-28" and p["stop_work_age"] == 58 and p["legal_pension_monthly"] == 1881.92
    assert p["investing_yearly"] == 30000 and p["spending_monthly"] is None
    app.act(demo_conn, "pension_profile", {"stop_work_age": "", "investing_yearly": ""})
    p = pension.profile(demo_conn)
    assert p["stop_work_age"] is None and p["investing_yearly"] is None
    app.act(demo_conn, "one_off", {"id": "car", "state": "in"})
    assert next(x for x in app.state(demo_conn, TODAY)["pension"]["big_expenses"] if x["id"] == "car")["state"] == "kept"
    app.act(demo_conn, "one_off", {"id": "car", "state": "auto"})
    assert next(x for x in app.state(demo_conn, TODAY)["pension"]["big_expenses"] if x["id"] == "car")["state"] == "auto"
    app.act(demo_conn, "pension_values", {"account": "IPT NN", "reserve": "101.234,56", "date": "2026-01-01",
                                          "expected": "412.345", "premium": ""})
    assert pension.profile(demo_conn)["accounts"]["IPT NN"] == {"expected_at_retirement": 412345.0}
    app.act(demo_conn, "tax_profile", {"municipal_rate": "7,5", "children": "1", "vvprbis": False})
    assert R.profile(demo_conn)["municipal_rate"] == 0.075
    with pytest.raises(ValueError):
        app.upload(demo_conn, "pension", "IPT NN", "fiche.csv", b"x")


def test_dashboard_has_r18_sections(demo_conn, tmp_path):
    depreciation.add_vehicle(demo_conn, "Spyder", 139000, "2026-07-16", 5, today=TODAY)
    p = dashboard.build(demo_conn, tmp_path / "d.html", TODAY)
    html = p.read_text(encoding="utf-8")
    assert "Verloning of dividend?" in html and "Extra aflossen of beleggen?" in html
    data = dashboard._extras(demo_conn, TODAY)
    assert data["vehicles"][0]["name"] == "Spyder"
    assert "error" not in (data["prepay"] or {})


def test_money_back_in_a_spending_category_is_a_one_off_too(demo_conn):
    from centen import pension, planner
    acc = demo_conn.execute("SELECT id FROM accounts WHERE account_type='current'").fetchone()[0]
    base = pension.spending_profile(demo_conn, TODAY)
    db.insert_transactions(demo_conn, [
        dict(id="buy", account_id=acc, booking_date="2026-07-16", amount=-139000.0, status="booked",
             counterparty_name="Porsche", merchant="Porsche", merchant_key="porsche", category="Car & maintenance",
             kind="expense", source="t"),
        dict(id="sell", account_id=acc, booking_date="2026-07-07", amount=100000.0, status="booked",
             counterparty_name="Garage", merchant="Garage", merchant_key="garage", category="Car & maintenance",
             kind="expense", source="t")])
    sp = pension.spending_profile(demo_conn, TODAY)
    assert {o["id"]: o["amount"] for o in sp["one_offs"]} == {"buy": 139000.0, "sell": -100000.0}
    assert sp["monthly"] == pytest.approx(base["monthly"], abs=0.05)  # neither the purchase nor the sale counts
    pension.set_profile(demo_conn, birth_date="1988-01-28")
    assert planner.inputs(demo_conn, TODAY)["one_offs_added_back_monthly"] == pytest.approx(39000 / 12, abs=1)
    big = {x["id"]: x for x in app.state(demo_conn, TODAY)["pension"]["big_expenses"]}
    assert big["sell"]["amount"] == -100000.0 and big["sell"]["state"] == "auto"


# --- r19: possessions -----------------------------------------------------------------------------------------

def test_possessions_are_listed_but_not_in_net_worth(demo_conn):
    from centen import analytics as A, possessions
    assets.add_account(demo_conn, "Broker", "investment", value=50000, value_date="2026-11-01", today=TODAY)
    before = A.position(demo_conn, today=TODAY)["net_worth"]["total"]
    inv_before = assets.summary(demo_conn, TODAY)["totals"]["investments_value"]
    assets.add_account(demo_conn, "Rolex Submariner", "asset", value=9500, value_date="2025-03-01", today=TODAY)
    depreciation.add_vehicle(demo_conn, "Porsche 718", 139000, "2026-07-16", 0, today=TODAY)
    T = assets.summary(demo_conn, TODAY)["totals"]
    assert T["investments_value"] == inv_before and T["possessions"] == pytest.approx(148500, abs=1)
    after = A.position(demo_conn, today=TODAY)["net_worth"]
    # r25: only real estate counts in the net worth; a car or a watch is listed, not counted
    assert after["total"] == pytest.approx(before, abs=1) and after["possessions"] == pytest.approx(148500, abs=1)
    o = possessions.overview(demo_conn, TODAY)
    kinds = {i["name"]: i["kind"] for i in o["items"]}
    assert kinds == {"Porsche 718": "Wagen", "Rolex Submariner": "Horloge & juwelen"}
    car = next(i for i in o["items"] if i["name"] == "Porsche 718")
    assert car["cost"] == 139000 and car["cost_basis"] == "aankoopprijs" and car["change"] == pytest.approx(0, abs=0.5)
    assert o["total"] == pytest.approx(148500, abs=1) and o["not_counted"] == pytest.approx(148500, abs=1)
    assert all(i["counted_in_net_worth"] is False and i["share_of_net_worth"] is None for i in o["items"])
    from centen import networth, scopes
    s = scopes.split(demo_conn, TODAY)
    assert s["pillars"]["wealth"]["possessions_not_counted"] == pytest.approx(148500, abs=1)
    assert s["total"] == pytest.approx(A.position(demo_conn, today=TODAY)["net_worth"]["total"] - (s["business"] or {}).get("owed_estimate", 0), abs=1)
    last = networth.reconstruct(demo_conn, TODAY)["last"]
    assert last["property"] == pytest.approx(assets.summary(demo_conn, TODAY)["totals"]["property"], abs=1)
    assert "Wagen" in o["running_costs_12m"]
