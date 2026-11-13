"""r15: capital gains tax, price history, net worth history, labelling 'Other', portfolio analysis, digest, CODA and
older bank history, and the dashboard sections built on them."""
import json
from datetime import date
from pathlib import Path

import pytest

from centen import analysis, assets, dashboard, db, digest, history, networth, review, tax
from centen.categorize import Context, enrich
from centen.demo import make_iban
from centen.importers import import_history
from centen.importers.coda import parse_coda

TODAY = date(2026, 11, 6)


def _inv(conn, acc_id, rows):
    for i, r in enumerate(rows):
        base = {"id": f"{acc_id}-{r['date']}-{r['kind']}-{i}", "account_id": acc_id, "time": None, "name": "World ETF", "isin": "IE00TEST0001",
                "symbol": None, "quantity": None, "price": None, "currency": "EUR", "amount": None, "amount_eur": None,
                "fee_eur": 0.0, "fx": None, "order_id": None, "description": None, "source": "degiro_transactions",
                "file": "t.csv"}
        base.update(r)
        cols = list(base)
        conn.execute(f"INSERT INTO inv_transactions({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                     [base[c] for c in cols])
    conn.commit()


@pytest.fixture()
def broker(conn):
    assets.add_account(conn, "DEGIRO", "investment", today=TODAY)
    acc = assets.find(conn, "DEGIRO")["id"]
    _inv(conn, acc, [
        {"date": "2020-03-01", "kind": "deposit", "amount_eur": 10000.0, "source": "degiro_account"},
        {"date": "2020-03-02", "kind": "buy", "quantity": 100, "price": 50.0, "amount_eur": -5000.0},   # lot 1 @50
        {"date": "2024-05-02", "kind": "buy", "quantity": 50, "price": 120.0, "amount_eur": -6000.0},   # lot 2 @120
        {"date": "2026-02-02", "kind": "buy", "quantity": 10, "price": 90.0, "amount_eur": -900.0},     # lot 3 (2026)
        {"date": "2026-06-01", "kind": "sell", "quantity": 120, "price": 110.0, "amount_eur": 13200.0},
    ])
    history.set_reference(conn, "IE00TEST0001", 100.0)  # 31/12/2025 closing price
    return conn, acc


def test_fifo_photo_moment_and_no_loss_from_historical_value(broker):
    conn, _ = broker
    cg = tax.capital_gains(conn, TODAY, price_lookup={"IE00TEST0001": {"price_eur": 110.0}})
    by_lot = [(d["acquired"], d["quantity"], d["acquisition_per_unit"], d["gain"], d["basis"]) for d in cg["disposals"]]
    # lot 1 (2020 @50): fotomoment 100 -> +10/unit; lot 2 (2024 @120 > 100), sold at 110: capped, no loss
    assert by_lot[0] == ("2020-03-02", 100, 100.0, 1000.0, "fotomoment 31/12/2025")
    assert by_lot[1][:4] == ("2024-05-02", 20, 110.0, 0.0) and "geen minwaarde" in by_lot[1][4]
    y = cg["years"][0]
    assert y["year"] == 2026 and y["net_gain"] == 1000.0 and y["remaining"] == 9000.0 and y["tax_estimate"] == 0.0
    left = {p["oldest_lot"]: p for p in cg["latent"]}
    assert sum(p["quantity"] for p in cg["latent"]) == pytest.approx(40)  # 30 of lot 2 + 10 of lot 3


def test_exemption_carry_over_chain():
    chain = tax._carry_chain({2026: 0.0, 2027: 10500.0}, 2027)
    assert chain[0]["carried_to_next_year"] == 1000.0          # nothing used: the first € 1.000 carries over
    assert chain[1]["used_basic"] == 10000.0 and chain[1]["used_carried"] == 500.0 and chain[1]["taxable"] == 0.0
    assert tax._carry_chain({2026: 0.0, 2027: 10500.0, 2028: 0.0}, 2028)[2]["exemption_carried_in"] == 500.0
    big = tax._carry_chain({2026: 25000.0}, 2026)[0]
    assert big["taxable"] == 15000.0 and big["tax_estimate"] == 1500.0 and big["carried_to_next_year"] == 0.0


def test_tax_report_checklist_and_foreign_broker(broker):
    conn, acc = broker
    _inv(conn, acc, [{"date": "2026-03-10", "kind": "dividend", "amount_eur": 50.0, "source": "degiro_account"}])
    rep = tax.report(conn, TODAY)
    assert rep["capital_gains"]["accounts"][0]["declare"] is True  # DEGIRO: foreign broker, declare yourself
    assert any("€ 1.000,00" in c for c in rep["checklist"])
    assert rep["dividends"]["years"][-1]["foreign_gross"] == 50.0


def test_price_history_monthly_series_and_close_on(conn):
    history.ensure(conn)
    conn.executemany("INSERT INTO price_history(key, date, close, currency, close_eur, interval, source) VALUES "
                     "(?,?,?,?,?,?,?)", [("X", "2026-01-31", 10, "EUR", 10, "1mo", "yahoo"),
                                         ("X", "2026-02-28", 12, "EUR", 12, "1mo", "yahoo"),
                                         ("X", "2025-12-30", 9, "EUR", 9, "1d", "yahoo")])
    assert history.monthly_series(conn, "X") == {"2025-12": 9, "2026-01": 10, "2026-02": 12}
    assert history.close_on(conn, "X", date(2025, 12, 31))["price_eur"] == 9


def test_review_queue_and_label_creates_rule(conn):
    me = make_iban("063", 1111111)
    db.upsert_account(conn, id="me", source="demo", bank="Belfius", name="Jan Test", iban=me)
    ctx = Context.from_db(conn)
    txs = [enrich({"id": f"t{i}", "account_id": "me", "booking_date": f"2026-0{i + 1}-10", "amount": -250.0,
                   "counterparty_name": "TUINWERKEN THOMAS", "remittance": "factuur tuin", "status": "booked",
                   "source": "demo"}, ctx) for i in range(3)]
    db.insert_transactions(conn, txs)
    conn.commit()
    q = review.queue(conn, "personal")
    item = q["items"][0]
    assert item["count"] == 3 and item["suggestion"]["category"] == "Home & DIY"
    dry = review.label(conn, item["merchant"], "Home & DIY", dry_run=True)
    assert dry["dry_run"] and dry["payments"] == 3 and dry["rule_pattern"]
    res = review.label(conn, item["merchant"], "Home & DIY")
    assert res["method"] == "rule" and res["not_changed"] == 0
    assert conn.execute("SELECT COUNT(*) FROM transactions WHERE category='Home & DIY'").fetchone()[0] == 3
    later = enrich({"id": "t9", "account_id": "me", "booking_date": "2026-09-10", "amount": -90.0,
                    "counterparty_name": "Tuinwerken Thomas", "status": "booked", "source": "demo"},
                   Context.from_db(conn))
    assert later["category"] == "Home & DIY"  # the rule also catches future payments
    with pytest.raises(ValueError):
        review.label(conn, "nobody at all", "Groceries")


def test_allocation_targets_and_plan():
    plan = analysis._plan({"etf": 800.0, "crypto": 200.0}, {"etf": 0.9, "crypto": 0.1}, 1000.0)
    assert plan["full_rebalance"] == {"crypto": -100.0, "etf": 100.0}
    assert plan["buy_with_new_money"] == {"crypto": 0.0, "etf": 1000.0}
    assert analysis.region_of("iShares Core MSCI World", "IE00B4L5Y983", "etf") == "Wereld"
    assert analysis.region_of("ISHRS MSCI CNTRY WRLD", "IE00B6R52259", "etf") == "Wereld"
    assert analysis.region_of("iShares $ Corp Bond", "IE0032895942", "etf") == "Obligaties"
    assert analysis.region_of("Intel", "US4581401001", "equity") == "VS"


def test_set_targets_validates(conn):
    with pytest.raises(ValueError):
        analysis.set_targets(conn, "class", {"etf": 70, "crypto": 10})
    assert analysis.set_targets(conn, "class", {"etf": 90, "crypto": 10})["targets"] == {"etf": 0.9, "crypto": 0.1}
    assert analysis.targets(conn)["class"]["etf"] == 0.9


def test_dividend_calendar_scales_to_quantity(broker):
    conn, acc = broker
    _inv(conn, acc, [{"date": "2026-03-10", "kind": "dividend", "amount_eur": 60.0, "source": "degiro_account"}])
    hs = [{"account": "DEGIRO", "key": "IE00TEST0001", "quantity": 40.0, "value": 4400.0}]
    cal = analysis.dividend_calendar(conn, TODAY, hs)
    # 60 on 160 units held on 10/3 -> 0,375/unit x 40 units = 15, expected March 2027
    assert cal["items"][0]["expected_gross"] == 15.0 and cal["items"][0]["expected_date"] == "2027-03-10"


def test_networth_reconstruct_and_snapshot(demo_conn):
    r = networth.reconstruct(demo_conn, TODAY)
    assert r["months"] > 6
    s = networth.snapshot(demo_conn, TODAY)
    tl = networth.timeline(demo_conn)
    assert tl["points"][-1]["source"] == "live" and tl["points"][-1]["total"] == s["total"]
    assert all(p["date"] < "2026-11" for p in tl["points"][:-1])


def test_digest_builds_and_renders(demo_conn, tmp_path):
    d, p = digest.write(demo_conn, TODAY, tmp_path)
    assert d["headline"] and p.read_text(encoding="utf-8").startswith("<!doctype html>")
    assert json.loads(p.with_suffix(".json").read_text(encoding="utf-8"))["today"] == TODAY.isoformat()


CODA = Path(__file__).parent / "fixtures" / "kbc.cod"


def test_coda_parser_reconciles_and_skips_globalisation_details():
    accounts, txs, balances = parse_coda(CODA)
    assert accounts[0]["id"] == "acc:BE68539007547034"
    assert [t["amount"] for t in txs] == [-42.5, 2500.0, -100.0]  # the two details of the 100 are not counted
    assert txs[1]["structured_ref"] == "+++123/4567/89002+++" and txs[0]["counterparty_name"] == "DELHAIZE LE LION"
    old, new = balances[0]["amount"], balances[-1]["amount"]
    assert round(old + sum(t["amount"] for t in txs), 2) == new


def test_import_history_leaves_linked_period_alone(conn):
    db.upsert_account(conn, id="acc:BE68539007547034", source="enablebanking", bank="KBC", name="KBC zicht",
                      iban="BE68539007547034")
    db.insert_transactions(conn, [{"id": "eb1", "account_id": "acc:BE68539007547034", "booking_date": "2026-06-03",
                                   "amount": 2500.0, "status": "booked", "source": "enablebanking"}])
    db.set_balance(conn, "acc:BE68539007547034", "2026-11-01", "booked", 9999.0)
    conn.commit()
    res = import_history(conn, [CODA])
    f = res["files"][0]
    assert f["format"] == "coda" and f["new_transactions"] == 1 and f["rows_from_linked_period_left_out"] == 2
    assert conn.execute("SELECT source, name FROM accounts WHERE id='acc:BE68539007547034'").fetchone()[:] == \
        ("enablebanking", "KBC zicht")
    latest = conn.execute("SELECT amount FROM balances WHERE account_id='acc:BE68539007547034' ORDER BY as_of DESC "
                          "LIMIT 1").fetchone()[0]
    assert latest == 9999.0
    assert import_history(conn, [CODA])["new_transactions"] == 0  # re-import adds nothing


def test_dashboard_has_r15_sections(demo_conn, tmp_path):
    p = dashboard.build(demo_conn, tmp_path / "d.html", today=TODAY)
    html = p.read_text(encoding="utf-8")
    data = json.loads(html.split('id="centen-data">')[1].split("</script>")[0].replace("<\\/", "</"))
    for k in ("networth_history", "tax", "analysis", "review"):
        assert k in data and not (isinstance(data[k], dict) and "error" in data[k]), (k, data[k])
    assert '["tax", "Fiscaal"]' in html


def test_r15_tools_are_registered():
    import asyncio
    from centen import server
    names = {t.name for t in asyncio.run(server.server.list_tools())}
    assert {"tax_overview", "set_reference_price", "fetch_price_history", "net_worth_history", "uncategorised_payments",
            "label_merchants", "portfolio_analysis", "set_allocation_targets", "set_instrument_region",
            "weekly_digest", "import_bank_history"} <= names


def test_networth_goes_back_with_investments_before_bank_history(broker):
    conn, acc = broker
    history.ensure(conn)
    conn.executemany("INSERT INTO price_history(key, date, close, currency, close_eur, interval, source) VALUES "
                     "(?,?,?,?,?,?,?)", [("IE00TEST0001", f"{y}-{m:02d}-28", 40.0 + y - 2020, "EUR", 40.0 + y - 2020,
                                          "1mo", "yahoo") for y in (2020, 2021, 2022) for m in range(1, 13)])
    db.upsert_account(conn, id="me", source="demo", bank="Belfius", name="Jan Test", iban=make_iban("063", 1))
    db.insert_transactions(conn, [{"id": "b1", "account_id": "me", "booking_date": "2022-06-01", "amount": 10.0,
                                   "status": "booked", "source": "demo"}])
    db.set_balance(conn, "me", "2026-11-01", "booked", 1000.0)
    conn.commit()
    networth.reconstruct(conn, TODAY)
    pts = {p["date"][:7]: p for p in networth.timeline(conn)["points"]}
    early = pts["2021-06"]
    assert early["total"] is None and early["cash_personal"] is None and early.get("no_bank_data")
    assert early["investments"] == pytest.approx(100 * 41.0 + 5000.0)  # 100 units x June 2021 price + broker cash
    assert pts["2022-06"]["total"] is not None


def test_month_bars_land_in_their_own_month():
    class R:
        status_code = 200

        def __init__(self, ts):
            self.ts = ts

        def json(self):
            return {"chart": {"result": [{"meta": {"currency": "EUR", "gmtoffset": 3600}, "timestamp": self.ts,
                                          "indicators": {"quote": [{"close": [1.0, 2.0]}]}}]}}

    class Http:
        def get(self, *a, **k):  # bars of 1 Oct and 1 Nov at midnight Amsterdam time (summer, then winter)
            return R([1790805600, 1793487600])

    s = history._yahoo_series(Http(), "X.AS", date(2026, 9, 1), date(2026, 11, 12), "1mo")
    assert s["closes"] == {date(2026, 10, 31): 1.0, date(2026, 11, 12): 2.0}


def test_loan_plan_keeps_balance_current_and_splits_interest(conn):
    assets.add_account(conn, "Woonkrediet 1", "loan", provider="Crelan", match=["123456789012"], today=TODAY)
    for i, d in enumerate(["2026-08-18", "2026-09-18", "2026-10-16"]):
        db.insert_transactions(conn, [{"id": f"l{i}", "account_id": "x", "booking_date": d, "amount": -743.61,
                                       "status": "booked", "source": "demo",
                                       "remittance": "DOSSIERNUMMER : 123456789012 Betaling kredietlasten"}])
    from centen.categorize import recategorize
    recategorize(conn)
    from centen import loans
    res = loans.set_terms(conn, "Woonkrediet 1", 28930.73, "2030-02-18", 0.14, 108648.33, "2016-06-09",
                          "2026-11-12", today=date(2026, 11, 12))
    assert res["instalment"] == 743.61 and res["instalment_source"] == "bank payments"
    assert res["next"]["interest"] == pytest.approx(28930.73 * 0.0014, abs=0.01)
    acc = assets.find(conn, "Woonkrediet 1")["id"]
    after = loans.balance_at(conn, acc, date(2026, 11, 20))
    assert after == pytest.approx(28930.73 * 1.0014 - 743.61, abs=0.01)
    assert loans.balance_at(conn, acc, date(2030, 3, 1)) == 0.0
    assert 108000 < loans.balance_at(conn, acc, date(2016, 7, 31)) <= 108648.33  # back to the start: the principal
    d = assets.describe(conn, acc, date(2026, 12, 1))
    assert d["outstanding"] == after and d["plan"]["payments_left"] == 39
    with pytest.raises(ValueError):
        loans.set_terms(conn, "Woonkrediet 1", 1, "2030-02-18", 9.0)


def test_update_app_actions_and_security(demo_conn, monkeypatch):
    import urllib.request
    from centen import app
    assert app.amount("40.000") == 40000.0 and app.amount("1.234,56") == 1234.56 and app.day("09/06/2016") == "2016-06-09"
    st = app.state(demo_conn, TODAY)
    assert {"savings", "assets", "review", "categories", "targets"} <= set(st)
    assets.add_account(demo_conn, "Pensioensparen", "pension", 1000.0, "2026-01-01", today=TODAY)
    res = app.act(demo_conn, "asset_value", {"account": "Pensioensparen", "value": "2.500,50", "date": "2026-11-01"})
    assert res["value"] == 2500.5
    with pytest.raises(ValueError):
        app.act(demo_conn, "nothing", {})
    app._server = None
    url = app.start(port=8795, open_browser=False)
    key = url.split("k=")[1]
    ok = urllib.request.urlopen(urllib.request.Request(url.split("?")[0].rstrip("/") + "/api/state",
                                                       headers={"X-Centen-Key": key}))
    assert ok.status == 200 and "assets" in json.loads(ok.read())
    for headers in ({}, {"X-Centen-Key": key, "Host": "evil.example"}):
        req = urllib.request.Request(url.split("?")[0].rstrip("/") + "/api/act/label", data=b"{}", headers=headers)
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req)
        assert e.value.code == 403
    app._server.shutdown()
    app._server = None
