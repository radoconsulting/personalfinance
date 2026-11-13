"""r17: fixed costs, cash forecast, pension overview, scenario planner, phone notifications and the PWA bits."""
import json
import os
import subprocess
from datetime import date
from pathlib import Path

import httpx
import pytest

from centen import assets, config, dashboard, db, fixed, forecast, notify, pension, planner, pwa

TODAY = date(2026, 11, 6)


@pytest.fixture()
def full(demo_conn):
    assets.add_account(demo_conn, "IPT NN", "pension", value=100000, value_date="2026-06-01", today=TODAY)
    assets.add_account(demo_conn, "Broker", "investment", value=200000, value_date="2026-11-01", today=TODAY)
    return demo_conn


# --- fixed costs -------------------------------------------------------------------------------

def test_fixed_costs_grouped_with_totals_and_signals(demo_conn):
    r = fixed.report(demo_conn, TODAY)
    p = r["totals"]["personal"]
    assert p["count"] > 5 and p["monthly"] > 0 and abs(p["yearly"] - sum(i["yearly"] for i in r["items"]
               if i["status"] == "active" and i["group"] != "Sparen & beleggen")) < 0.05
    groups = {i["name"]: i["group"] for i in r["items"]}
    assert groups["Netflix"] == "Abonnementen" and groups["Engie"] == "Wonen & energie"
    assert any(x["name"] == "Engie" and x["direction"] == "up" for x in r["price_changes"])
    assert any(o["category"] == "Subscriptions & streaming" and len(o["names"]) >= 2 for o in r["overlaps"])
    assert all(b["amount"] >= 100 for b in r["big_coming"])
    assert r["fixed_share_of_income"] and 0 < r["fixed_share_of_income"] < 1
    assert all("account_id" not in i for i in r["items"])


def test_ignore_cancel_and_manual_fixed_costs(demo_conn):
    before = fixed.report(demo_conn, TODAY)["totals"]["personal"]["monthly"]
    fixed.set_item(demo_conn, "Netflix", "cancelled", today=TODAY)
    fixed.set_item(demo_conn, "Disney+", "ignored", today=TODAY)
    r = fixed.report(demo_conn, TODAY)
    names = {i["name"]: i for i in r["items"]}
    assert "Disney+" not in names and names["Netflix"]["status"] == "cancelled" and names["Netflix"]["monthly"] == 0
    assert r["totals"]["personal"]["monthly"] < before
    fixed.set_item(demo_conn, "Netflix", "active", today=TODAY)
    assert {i["name"]: i for i in fixed.items(demo_conn, TODAY)}["Netflix"]["status"] == "active"
    fixed.set_item(demo_conn, "Spotify", new_name="Spotify gezin", group="Lidmaatschappen", today=TODAY)
    assert any(i["name"] == "Spotify gezin" and i["group"] == "Lidmaatschappen" for i in fixed.items(demo_conn, TODAY))
    add = fixed.add_manual(demo_conn, "Krant via Visa", 299, "jaarlijks", "2027-01-15", "Abonnementen")
    assert add["cadence"] == "yearly" and add["yearly"] == pytest.approx(299, abs=1)
    pays = fixed.expected_payments(demo_conn, date(2026, 11, 7), date(2027, 3, 1), TODAY)
    assert [p["date"] for p in pays] == ["2027-01-15"]
    with pytest.raises(ValueError):
        fixed.add_manual(demo_conn, "x", 10, "fortnightly-ish")
    with pytest.raises(ValueError):
        fixed.set_item(demo_conn, "does not exist", "ignored", today=TODAY)
    fixed.set_item(demo_conn, "Krant via Visa", "cancelled", today=TODAY)
    assert fixed.expected_payments(demo_conn, date(2026, 11, 7), date(2027, 3, 1), TODAY) == []


# --- forecast ---------------------------------------------------------------------------------

def test_forecast_shapes_and_consistency(demo_conn):
    f = forecast.forecast(demo_conn, TODAY, 6)
    p = f["perspectives"]["personal"]
    assert f["perspectives"]["business"] is None  # no company accounts in the demo
    assert p["start"] == pytest.approx(sum(a["start"] for a in f["accounts"] if a["scope"] == "personal"), abs=0.05)
    assert p["end"] == pytest.approx(sum(a["end"] for a in f["accounts"] if a["scope"] == "personal"), abs=0.05)
    assert p["min"] <= min(x["balance"] for x in p["series"]) + 0.01
    assert p["series"][0]["date"] == TODAY.isoformat() and p["series"][-1]["date"] == f["to"]
    salary = [e for e in f["events_next_60_days"] if e["category"] == "Salary"]
    assert salary and all(e["amount"] > 0 for e in salary)
    assert all(e["date"] > TODAY.isoformat() for e in f["events_next_60_days"])
    months = [m for m in f["monthly"] if m["perspective"] == "personal"]
    assert months[0]["partial"] and months[-1]["partial"]
    assert any(v["category"] == "Groceries" and v["monthly"] < 0 for v in f["variable"])


def test_forecast_respects_cancelled_and_manual_items_and_warns(demo_conn, monkeypatch):
    f0 = forecast.forecast(demo_conn, TODAY, 3)
    fixed.set_item(demo_conn, "Kinderdagverblijf", "cancelled", today=TODAY)
    f1 = forecast.forecast(demo_conn, TODAY, 3)
    assert not any("Kinderdagverblijf" in (e["name"] or "") for e in f1["events_next_60_days"])
    assert f1["perspectives"]["personal"]["end"] > f0["perspectives"]["personal"]["end"] + 700
    fixed.add_manual(demo_conn, "Grote factuur", 40000, "yearly", "2026-12-01")
    f2 = forecast.forecast(demo_conn, TODAY, 3)
    assert f2["perspectives"]["personal"]["min_date"] >= "2026-12-01"
    assert any(w["scope"] == "personal" for w in f2["warnings"])
    assert any(e["kind"] == "manual" for e in f2["events_next_60_days"])


def test_forecast_seasonal_items_come_back_a_year_later(demo_conn):
    acc = demo_conn.execute("SELECT id FROM accounts WHERE account_type='current'").fetchone()[0]
    db.insert_transactions(demo_conn, [dict(id="tax2025", account_id=acc, booking_date="2025-12-10", amount=-1234.0,
                                            status="booked", counterparty_name="FOD Financien", merchant="FOD Financiën",
                                            merchant_key="fod", category="Taxes & social security", kind="expense",
                                            source="test")])
    f = forecast.forecast(demo_conn, TODAY, 3)
    seas = [e for e in f["events_next_60_days"] if e["kind"] == "seasonal"]
    assert seas and seas[0]["date"] == "2026-12-10" and seas[0]["amount"] == -1234.0


# --- pension ----------------------------------------------------------------------------------

def test_pension_needs_profile_then_projects(full):
    o = pension.overview(full, TODAY)
    assert o["years_to_retirement"] is None and o["needs"]
    assert any(c["kind"] == "pension_saving_missing" for c in o["checks"])
    pension.set_profile(full, birth_date="1988-01-28", legal_pension_monthly=2100,
                        accounts={"IPT NN": {"premium_yearly": 5000, "pillar": 2}}, return_pension=2.5)
    o = pension.overview(full, TODAY)
    assert o["retirement_date"] == "2055-02-01" and 28 < o["years_to_retirement"] < 29
    pil = {p["pillar"]: p for p in o["pillars"]}
    assert pil[1]["monthly_today_eur"] == 2100
    ipt = pil[2]["accounts"][0]
    assert ipt["premium_assumed"] == 5000 and ipt["return_assumed"] == 0.025
    assert ipt["projected_nominal"] > 100000 + 5000 * 28
    assert ipt["projected_today_eur"] < ipt["projected_nominal"]
    assert pil[4]["accounts"][0]["name"] == "Broker"
    total = sum(p["monthly_today_eur"] or 0 for p in o["pillars"])
    assert o["monthly_income_today_eur"] == pytest.approx(total, abs=0.05)
    assert o["replacement_ratio"] == pytest.approx(o["monthly_income_today_eur"] / o["spending_monthly_now"], abs=0.001)
    pension.set_profile(full, accounts={"IPT NN": {"expected_at_retirement": 300000}})
    ipt = [p for p in pension.overview(full, TODAY)["pillars"] if p["pillar"] == 2][0]["accounts"][0]
    assert ipt["expected_at_retirement"] == 300000
    with pytest.raises(ValueError):
        pension.set_profile(full, retirement_age=20)


def test_pension_pillar_detection():
    assert pension.pillar_of({"name": "Pensioensparen Argenta", "type": "pension"}, {}) == 3
    assert pension.pillar_of({"name": "IPT NN Scala", "type": "pension"}, {}) == 2
    assert pension.pillar_of({"name": "Tak 23", "type": "insurance"}, {}) == 4
    assert pension.pillar_of({"name": "X", "type": "pension"}, {"pillar": 3}) == 3


# --- planner ----------------------------------------------------------------------------------

def test_planner_scenarios_move_the_right_way(full):
    pension.set_profile(full, birth_date="1988-01-28", legal_pension_monthly=2100)
    r = planner.plan(full, TODAY)
    base = r["projection"]
    assert base["capital_now"] == pytest.approx(r["inputs"]["cash_personal"] + 200000, abs=1)
    sc = {s["name"]: s for s in r["scenarios"]}
    assert sc["€ 500 per maand meer sparen"]["capital_at_retirement"] > base["capital_at_retirement"]
    assert sc["10 % minder uitgeven"]["fi_year"] <= base["fi_year"]
    assert sc["Lager rendement (3 %)"]["capital_at_retirement"] < base["capital_at_retirement"]
    assert "Leningen nu aflossen" not in sc  # demo has no loan with a plan
    early = planner.project(r["inputs"], stop_work_age=45)
    assert early["capital_at_retirement"] < base["capital_at_retirement"]


def test_planner_js_engine_matches_python(full, tmp_path):
    if not shutil_which("node"):
        pytest.skip("node not installed")
    pension.set_profile(full, birth_date="1988-01-28", legal_pension_monthly=2100)
    inp = planner.inputs(full, TODAY)
    inp["loans"] = [{"name": "L", "instalment_monthly": 800, "ends": "2034-12-18", "outstanding": 60000}]
    tpl = dashboard.TEMPLATE.read_text(encoding="utf-8")
    js = tpl[tpl.index("function planPensionCapital"):tpl.index("function viewPlanner")]
    pension.set_profile(full, accounts={"IPT NN": {"premium_yearly": 5000}})
    inp = planner.inputs(full, TODAY)
    inp["loans"] = [{"name": "L", "instalment_monthly": 800, "ends": "2034-12-18", "outstanding": 60000}]
    early = dict(inp, stop_work_age=58)
    for i2, py, jsc in [(early, {}, {}), (early, {"stop_work_age": 67}, {"stop": None})]:
        p = planner.project(i2, **py)
        code = js + f"\nconst r=planProject({json.dumps(i2)},{json.dumps(jsc)});" \
                    "console.log(JSON.stringify([r.fi&&r.fi.year, r.atRet, r.rows[r.rows.length-1].capital]))"
        out = json.loads(subprocess.run(["node", "-e", code], capture_output=True, text=True, check=True).stdout)
        assert out[0] == p["fi_year"] and out[1] == pytest.approx(p["capital_at_retirement"], abs=1)
        assert out[2] == pytest.approx(p["end_capital"], abs=1)
    for py, jsc in [({}, {}), ({"extra_monthly_saving": 500}, {"extra": 500}), ({"pay_off_loans_now": True}, {"payoff": True}),
                    ({"stop_work_age": 50, "return_": 0.04}, {"stop": 50, "ret": 0.04}),
                    ({"spending_change_pct": -0.2}, {"spend": -0.2})]:
        p = planner.project(inp, **py)
        code = js + f"\nconst r=planProject({json.dumps(inp)},{json.dumps(jsc)});" \
                    "console.log(JSON.stringify([r.fi&&r.fi.year, r.atRet, r.rows[r.rows.length-1].capital]))"
        out = json.loads(subprocess.run(["node", "-e", code], capture_output=True, text=True, check=True).stdout)
        assert out[0] == p["fi_year"]
        assert out[1] == pytest.approx(p["capital_at_retirement"], abs=1)
        assert out[2] == pytest.approx(p["end_capital"], abs=1)


def shutil_which(x):
    import shutil
    return shutil.which(x)


# --- dashboard --------------------------------------------------------------------------------

def test_dashboard_has_r17_sections(full, tmp_path):
    p = dashboard.build(full, tmp_path / "d.html", TODAY)
    html = p.read_text(encoding="utf-8")
    for tab in ('["fixed", "Vaste kosten"]', '["forecast", "Prognose"]', '["pension", "Pensioen"]', '["planner", "Planner"]'):
        assert tab in html
    data = dashboard.collect(full, TODAY)
    data.update(dashboard._extras(full, TODAY))
    for k in ("fixed", "forecast", "pension", "planner"):
        assert k in data and "error" not in (data[k] or {}), k


# --- notifications ----------------------------------------------------------------------------

def _mock(sent):
    def handler(req):
        sent.append(json.loads(req.content))
        return httpx.Response(200, json={"id": "x"})
    return httpx.Client(transport=httpx.MockTransport(handler))


def _double_payment(conn):
    acc = conn.execute("SELECT id FROM accounts WHERE account_type='current'").fetchone()[0]
    db.insert_transactions(conn, [dict(id=f"dup{i}", account_id=acc, booking_date="2026-11-05", amount=-89.0,
                                       status="booked", counterparty_name="Fnac", merchant="Fnac", merchant_key="fnac",
                                       category="Electronics", kind="expense", source="test") for i in (1, 2)])


def test_notifications_short_by_default_and_sent_once(demo_conn):
    sent = []
    _double_payment(demo_conn)
    first = notify.run_alerts(demo_conn, TODAY, client=_mock(sent))
    assert first["new"] > 0 and first["sent"] is False and not sent  # not configured
    notify.setup("centen-test-topic-123456")
    with pytest.raises(ValueError):
        notify.setup("short")
    r = notify.run_alerts(demo_conn, TODAY, client=_mock(sent))
    assert r["new"] > 0 and r["sent"] and sent
    msg = sent[-1]
    assert msg["topic"] == "centen-test-topic-123456" and msg["title"].startswith("centen:")
    assert "Mogelijk dubbele betaling" in msg["message"] and "Fnac" not in msg["message"]  # short: no names
    assert msg["priority"] == 4
    again = notify.run_alerts(demo_conn, TODAY, client=_mock(sent))
    assert again["new"] == 0 and len(sent) == 1
    notify.setup("centen-test-topic-123456", detail="full", public_url="https://centen.example")
    assert notify.send("t", "m", client=_mock(sent)) and sent[-1]["click"] == "https://centen.example"
    assert notify.test(client=_mock(sent))["sent"]


def test_notification_failure_never_raises(demo_conn):
    notify.setup("centen-test-topic-123456")
    _double_payment(demo_conn)

    def boom(req):
        raise httpx.ConnectError("down")
    assert notify.send("t", "m", client=httpx.Client(transport=httpx.MockTransport(boom))) is False
    r = notify.run_alerts(demo_conn, TODAY, client=httpx.Client(transport=httpx.MockTransport(boom)))
    assert r["sent"] is False
    assert r["new"] > 0 and notify.run_alerts(demo_conn, TODAY, dry_run=True)["new"] == r["new"]  # not marked


# --- PWA --------------------------------------------------------------------------------------

def test_icons_are_valid_pngs():
    for n in pwa.ICONS:
        b = pwa.icon(n)
        assert b[:8] == b"\x89PNG\r\n\x1a\n" and len(b) > 300
    assert pwa.icon("nope.png") is None
    assert '<link rel="manifest"' in pwa.inject("<html><head></head><body></body></html>")


# --- r17b: one-offs out of the spending profile, investing pattern in the pension projection ------------------

def _big(conn, tid, d, amount, merchant="Porsche Center", category="Car & maintenance"):
    acc = conn.execute("SELECT id FROM accounts WHERE account_type='current'").fetchone()[0]
    db.insert_transactions(conn, [dict(id=tid, account_id=acc, booking_date=d, amount=amount, status="booked",
                                       counterparty_name=merchant, merchant=merchant,
                                       merchant_key=merchant.lower().split()[0], category=category, kind="expense",
                                       source="test")])


def test_one_offs_left_out_of_spending_profile(full):
    base = pension.spending_profile(full, TODAY)
    _big(full, "car", "2026-07-16", -139000.0)
    _big(full, "tax", "2026-09-29", -17538.85, "Vlaamse Belastingdienst", "Taxes & social security")
    _big(full, "wrap", "2026-08-10", -4779.5, "DRX Exclusive")
    # a yearly bill of the same size a year earlier is not a one-off
    _big(full, "ins1", "2025-10-01", -12000.0, "Big Insurer", "Insurance")
    _big(full, "ins2", "2026-10-01", -12100.0, "Big Insurer", "Insurance")
    sp = pension.spending_profile(full, TODAY)
    ids = {o["id"] for o in sp["one_offs"]}
    assert ids == {"car", "tax"}
    assert sp["monthly_raw"] == pytest.approx(base["monthly_raw"] + (139000 + 17538.85 + 4779.5 + 12100) / 12, abs=0.05)
    assert sp["monthly"] == pytest.approx(sp["monthly_raw"] - (139000 + 17538.85) / 12, abs=0.05)
    pension.mark_one_off(full, ["wrap"], True)
    pension.mark_one_off(full, ["tax"], False)
    ids = {o["id"]: o["reason"] for o in pension.spending_profile(full, TODAY)["one_offs"]}
    assert ids == {"car": "auto", "wrap": "manual"}
    pension.mark_one_off(full, ["tax"], None)
    assert "tax" in {o["id"] for o in pension.spending_profile(full, TODAY)["one_offs"]}
    with pytest.raises(ValueError):
        pension.mark_one_off(full, ["nope"])
    pension.set_profile(full, spending_monthly=5000)
    o = pension.overview(full, TODAY)
    assert o["spending_monthly_now"] == 5000 and o["spending_profile"]["override"]
    pension.set_profile(full, reset=["spending_monthly"])
    assert not pension.overview(full, TODAY)["spending_profile"]["override"]
    # the planner uses the same profile and adds the one-offs back to what you can save
    pension.set_profile(full, birth_date="1988-01-28")
    inp = planner.inputs(full, TODAY)
    assert inp["annual_spending"] == pytest.approx(pension.spending_profile(full, TODAY)["monthly"] * 12, abs=1)
    assert inp["one_offs_added_back_monthly"] > 0


def test_investing_pattern_feeds_pillar_4(full):
    pension.set_profile(full, birth_date="1988-01-28")
    acc = assets.find(full, "Broker")["id"]
    rows = [("d1", "2022-03-01", 20000.0), ("d2", "2024-05-01", 30000.0), ("d3", "2026-02-01", 50000.0),
            ("w1", "2025-06-01", -10000.0)]
    for i, d, amt in rows:
        full.execute("INSERT INTO inv_transactions(id, account_id, date, kind, amount, amount_eur, currency, source) "
                     "VALUES (?,?,?,?,?,?,'EUR','test')", (i, acc, d, "deposit" if amt > 0 else "withdrawal", amt, amt))
    full.commit()
    o = pension.overview(full, TODAY)
    inv = o["investing"]
    assert {r["year"]: r["net"] for r in inv["per_year"]} == {"2022": 20000.0, "2024": 30000.0, "2025": -10000.0, "2026": 50000.0}
    assert inv["yearly"] == pytest.approx(90000 / ((TODAY - date(2022, 3, 1)).days / 365.25), abs=1) and not inv["override"]
    p4 = [p for p in o["pillars"] if p["pillar"] == 4][0]
    new = [r for r in p4["accounts"] if r["type"] == "contributions"][0]
    assert new["premium_assumed"] == inv["yearly"] and new["projected_nominal"] > inv["yearly"] * 28
    pension.set_profile(full, investing_yearly=0)
    p4 = [p for p in pension.overview(full, TODAY)["pillars"] if p["pillar"] == 4][0]
    assert not any(r["type"] == "contributions" for r in p4["accounts"])


# --- r17c: saving stops when you stop working ----------------------------------------------------------------

def test_stop_work_age_stops_premiums_and_investing(full):
    pension.set_profile(full, birth_date="1988-01-28", legal_pension_monthly=1800,
                        accounts={"IPT NN": {"premium_yearly": 5000, "expected_at_retirement": 400000}},
                        investing_yearly=30000)
    full_career = pension.overview(full, TODAY)
    pension.set_profile(full, stop_work_age=55)
    early = pension.overview(full, TODAY)
    assert early["stop_work_age"] == 55 and 16 < early["years_paying"] < 17
    p2 = lambda o: [p for p in o["pillars"] if p["pillar"] == 2][0]
    p4 = lambda o: [p for p in o["pillars"] if p["pillar"] == 4][0]
    ipt_full, ipt_early = p2(full_career)["accounts"][0], p2(early)["accounts"][0]
    assert ipt_full["expected_at_retirement"] == 400000 and ipt_early["expected_at_retirement"] is None
    assert ipt_early["projected_nominal"] == pytest.approx(
        pension._fv(100000, 5000, 0.025, early["years_to_retirement"], early["years_paying"]), rel=0.005)
    assert ipt_early["projected_nominal"] < pension._fv(100000, 5000, 0.025, early["years_to_retirement"])
    rows = {r["type"]: r for r in p4(early)["accounts"]}
    assert rows["contributions"]["name"] == "Nieuwe inleg tot je 55 jaar"
    assert rows["drawdown"]["projected_today_eur"] < 0
    assert p4(early)["projected_today_eur"] < p4(full_career)["projected_today_eur"]
    # the planner follows the profile: no saving after 55 and smaller pension capital
    inp = planner.inputs(full, TODAY)
    assert inp["stop_work_age"] == 55
    pr = planner.project(inp)
    after = [r for r in pr["years"] if r["age"] and 56 <= r["age"] < 66]
    assert after and all(r["phase"] == "living off capital" for r in after)
    assert planner.pension_capital(inp, 55, inp["inflation"]) < planner.pension_capital(inp, None, inp["inflation"])
    assert planner.pension_capital(inp, None, inp["inflation"]) == pytest.approx(
        400000 / (1 + inp["inflation"]) ** (67 - inp["age"]) * (1 - 0.165), rel=0.01)
    pension.set_profile(full, reset=["stop_work_age"])
    assert pension.overview(full, TODAY)["stop_work_age"] == 67
