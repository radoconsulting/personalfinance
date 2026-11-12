"""Investment, pension, property and loan accounts (assets.py), positions import and their place in the totals."""
from datetime import date

import pytest

from centen import analytics as A
from centen import assets, dashboard, db, manual, positions
from centen.categorize import Context, enrich
from test_business import BROKER_DE, biz, iban  # noqa: F401  (fixture)

TODAY = date(2026, 11, 6)


def _add_tx(conn, *txs):
    ctx = Context.from_db(conn)
    db.insert_transactions(conn, [enrich(dict(t, status="booked", currency="EUR", source="test"), ctx) for t in txs])
    conn.commit()


def _tx(i, acc, d, amount, cp=None, cp_iban=None, msg=None, desc=None):
    return dict(id=f"a{i}", account_id=acc, booking_date=d, amount=amount, counterparty_name=cp,
                counterparty_iban=cp_iban, remittance=msg, description=desc)


def _row(conn, tid):
    return conn.execute("SELECT category, kind, internal, asset_id, merchant FROM transactions WHERE id=?",
                        (tid,)).fetchone()


def test_broker_suggested_then_added_takes_over_transfers(biz):  # noqa: F811
    sug = [s for s in assets.suggestions(biz) if s["destination"] == "DEGIRO"]
    assert sug and sug[0]["net_deposited"] == 20000.0 and sug[0]["type_guess"] == "investment"
    cash_before = A.totals(biz)["net_cash"]
    res = assets.add_account(biz, "DEGIRO", "investment", 23500.0, "2026-11-01", today=TODAY)
    assert res["contributions"] == 20000.0 and res["value"] == 23500.0 and res["gain"] == 3500.0
    assert res["gain_pct"] == pytest.approx(0.175) and res["payments"] == 1 and "took_over" in res
    assert res["recognised_by"] and all("account" in x or "pattern" in x for x in res["recognised_by"])
    cat, kind, internal, asset_id, merchant = _row(biz, "t15")  # the transfer to the DEGIRO account
    assert (cat, kind, asset_id, merchant) == ("Savings & investments", "transfer", "asset:degiro", "DEGIRO")
    assert A.totals(biz)["net_cash"] == cash_before  # never part of cash
    assert not [s for s in assets.suggestions(biz) if s["destination"] == "DEGIRO"]
    pos = A.position(biz, date(2026, 1, 1), TODAY, TODAY)
    assert pos["investments"]["value"] == 23500.0
    assert pos["net_worth"]["total"] == pytest.approx(cash_before + 23500.0)
    assert [d for d in pos["investments"]["by_destination"] if d["destination"] == "DEGIRO"][0]["added"]


def test_value_is_estimated_from_later_deposits_until_a_new_value(biz):  # noqa: F811
    assets.add_account(biz, "DEGIRO", "investment", 23500.0, "2026-11-01", today=TODAY)
    _add_tx(biz, _tx(1, "me", "2026-11-03", -1000.0, "Jan Test", BROKER_DE))
    a = assets.describe(biz, "asset:degiro", TODAY)
    assert a["estimated"] and a["value"] == 24500.0 and a["moved_since_value"] == 1000.0
    assert a["contributions"] == 21000.0 and a["gain"] == 3500.0
    a = assets.set_value(biz, "degiro", 25010.55, "2026-11-05", today=TODAY)
    assert not a["estimated"] and a["value"] == 25010.55 and a["gain"] == pytest.approx(4010.55)


def test_contributions_before_the_bank_history(biz):  # noqa: F811
    assets.add_account(biz, "DEGIRO", "investment", 100000.0, "2026-11-01", contributions=80000.0,
                       contributions_date="2026-11-01", today=TODAY)
    _add_tx(biz, _tx(2, "me", "2026-11-04", -5000.0, "Jan Test", BROKER_DE))
    a = assets.describe(biz, "asset:degiro", TODAY)
    assert a["contributions"] == 85000.0 and a["value"] == 105000.0 and a["gain"] == 20000.0
    # the entered total moves back in time with the payments: before the August transfer it was 60.000
    acc = assets.find(biz, "degiro")
    flows = assets._flows(biz, acc["id"])
    entered = assets._entries(biz, acc["id"], assets.CONTRIBUTIONS)
    assert assets._contrib_at(entered, flows, date(2026, 7, 31)) == 60000.0
    tl = assets.timeline(biz, TODAY)
    assert tl["months"][0] == "2026-08" and tl["contributions"][0] == 80000.0
    assert tl["contributions"][-1] == 85000.0 and tl["value"][-1] == 105000.0 and tl["value"][0] is None


def test_company_paid_pension_stays_a_company_cost(biz):  # noqa: F811
    _add_tx(biz, _tx(3, "bv", "2026-10-01", -5760.88, "NN Insurance Belgium", None,
                     "Premie d.d. 01.10.2026 Scala Privilege Domiciliëring"),
            _tx(30, "bv", "2025-10-01", -5758.10, "NN Insurance Belgium", None,
                "Premie d.d. 01.10.2025 Scala Privilege Domiciliëring"))
    before = _row(biz, "a3")
    assert before["category"] == "Insurance"
    sug = [s for s in assets.suggestions(biz) if s["destination"] == before["merchant"]]
    assert sug and sug[0]["type_guess"] == "pension" and sug[0]["paid_from"] == ["business"]
    res = assets.add_account(biz, "IPT NN", "pension", 30000.0, "2026-01-01", match=["scala privilege"],
                             scope="personal", today=TODAY)
    assert res["contributions_by_payer"] == {"business": pytest.approx(11518.98)}
    assert res["value"] == pytest.approx(35760.88) and res["estimated"]
    cat, kind, internal, asset_id, _ = _row(biz, "a3")
    assert (cat, kind, internal, asset_id) == ("Insurance", "expense", "cross", "asset:ipt-nn")
    pos = A.position(biz, date(2026, 1, 1), TODAY, TODAY)
    assert pos["business"]["paid_into_your_pension"] == 5760.88
    assert pos["company_to_you"]["into_your_pension"] == 5760.88
    assert 5760.88 <= pos["business"]["other_costs"]  # still a cost for the company
    total = A.breakdown(biz, date(2026, 1, 1), TODAY, perspective="total")
    assert all(i["key"] != "Insurance" for i in total["items"])  # family view: savings, not spending


def test_personal_pension_savings_are_not_spending(biz):  # noqa: F811
    _add_tx(biz, _tx(4, "me", "2026-09-15", -1020.0, "AG Insurance", None, "Pensioensparen 2026 contract 123"))
    assets.add_account(biz, "Pensioensparen AG", "pension", 14000.0, match=["pensioensparen"], today=TODAY)
    cat, kind, internal, asset_id, merchant = _row(biz, "a4")
    assert (cat, kind, asset_id, merchant) == ("Savings & investments", "transfer", "asset:pensioensparen-ag",
                                               "Pensioensparen AG")


def test_loan_keeps_its_category_and_lowers_net_worth(biz):  # noqa: F811
    _add_tx(biz, _tx(5, "joint", "2026-10-02", -1338.94, "Crelan", None, "Aflossing krediet 123/456"))
    cat_before = _row(biz, "a5")["category"]
    cash = A.totals(biz)["net_cash"]
    res = assets.add_account(biz, "Woonkrediet", "loan", -180000.0, "2026-09-01", match=["aflossing krediet"],
                             today=TODAY)
    assert res["outstanding"] == 180000.0 and res["paid_since_value"] == 1338.94
    assert _row(biz, "a5")["category"] == cat_before and _row(biz, "a5")["asset_id"] == "asset:woonkrediet"
    pos = A.position(biz, date(2026, 1, 1), TODAY, TODAY)
    assert pos["net_worth"]["loans"] == -180000.0
    assert pos["net_worth"]["total"] == pytest.approx(cash + 20000.0 - 180000.0)  # DEGIRO not added: net deposits


def test_remove_restores_categories(biz):  # noqa: F811
    _add_tx(biz, _tx(6, "me", "2026-09-15", -500.0, "AG Insurance", None, "Pensioensparen"))
    before = tuple(_row(biz, "a6"))
    assets.add_account(biz, "Pensioen", "pension", 1000.0, match=["pensioensparen"], today=TODAY)
    assert _row(biz, "a6")["asset_id"]
    assert assets.remove(biz, "pensioen")["removed"] == "Pensioen"
    assert tuple(_row(biz, "a6")) == before
    assert not biz.execute("SELECT 1 FROM asset_links").fetchone()


def test_errors(biz):  # noqa: F811
    with pytest.raises(ValueError):
        assets.add_account(biz, "X", "boat")
    assets.add_account(biz, "DEGIRO", "investment", 1.0, today=TODAY)
    with pytest.raises(ValueError):
        assets.add_account(biz, "degiro", "investment", today=TODAY)
    with pytest.raises(ValueError):
        assets.find(biz, "nothing like this")
    me = biz.execute("SELECT iban FROM accounts WHERE id='me'").fetchone()[0]
    with pytest.raises(ValueError):
        assets.add_account(biz, "Own", "investment", counterparty_accounts=[me], today=TODAY)
    with pytest.raises(ValueError):  # the broker IBAN belongs to DEGIRO now, not a savings account
        manual.add_account(biz, BROKER_DE, 1.0)


def test_search_by_asset_and_hidden_ibans(biz):  # noqa: F811
    assets.add_account(biz, "DEGIRO", "investment", 1.0, today=TODAY)
    res = A.search(biz, account="degiro")
    assert res["matches"] == 1 and res["transactions"][0]["amount"] == -20000.0
    ro = db.connect_readonly(biz.execute("PRAGMA database_list").fetchone()[2])
    assert all(r[0] is None for r in ro.execute("SELECT iban FROM asset_links"))
    assert ro.execute("SELECT COUNT(*) FROM asset_links WHERE iban_masked IS NOT NULL").fetchone()[0] >= 1


# --- positions -----------------------------------------------------------------------

DEGIRO_CSV = """Product,Symbool/ISIN,Aantal,Slotkoers,Lokale waarde,,Waarde in EUR
CASH & CASH FUND & FTX CASH (EUR),,,,EUR,"1.234,56","1.234,56"
VANGUARD FTSE ALL-WORLD UCITS ETF USD ACC,IE00BK5BQT80,250,"128,40",EUR,"32100,00","32100,00"
ISHARES CORE S&P 500 UCITS ETF USD (DIST),IE0031442068,900,"52,10",USD,"46890,00","40213,55"
"""

IBKR_CSV = """Statement,Header,Field Name,Field Value
Statement,Data,Period,"January 1, 2026 - October 31, 2026"
Account Information,Header,Field Name,Field Value
Account Information,Data,Base Currency,EUR
Net Asset Value,Header,Asset Class,Prior Total,Current Long,Current Short,Current Total,Change
Net Asset Value,Data,Cash ,100,250.5,0,250.5,150.5
Net Asset Value,Data,Stock,1000,9416.6,0,9416.6,8416.6
Net Asset Value,Data,Total,1100,9667.1,0,9667.1,8567.1
Open Positions,Header,DataDiscriminator,Asset Category,Currency,Symbol,Quantity,Mult,Cost Price,Cost Basis,Close Price,Value,Unrealized P/L,Code
Open Positions,Data,Summary,Stocks,USD,VWRA,40,1,120,4800,140,5600,800,
Open Positions,Data,Summary,Stocks,EUR,IWDA,40,1,90,3600,100,4000,400,
Open Positions,Total,,Stocks,USD,,,,,4800,,5600,800,
Base Currency Exchange Rate,Header,Currency,Rate
Base Currency Exchange Rate,Data,USD,0.86
Financial Instrument Information,Header,Asset Category,Symbol,Description,Conid,Security ID,Listing Exch,Multiplier,Type,Code
Financial Instrument Information,Data,Stocks,VWRA,VANGUARD FTSE ALL-WORLD USD A,1,IE00BK5BQT80,LSEETF,1,ETF,
"""


def test_parse_degiro_portfolio():
    res = positions.parse_text(DEGIRO_CSV)
    assert res["format"] == "degiro" and len(res["positions"]) == 3 and not res["warnings"]
    cash, vwce, sp = res["positions"]
    assert cash["value"] == 1234.56 and cash["isin"] is None
    assert vwce["isin"] == "IE00BK5BQT80" and vwce["quantity"] == 250 and vwce["value"] == 32100.0
    assert sp["currency"] == "USD" and sp["value_local"] == 46890.0 and sp["value"] == 40213.55


def test_parse_semicolon_table_and_missing_eur():
    res = positions.parse_text("Naam;ISIN;Aantal;Koers;Munt;Waarde\nAXA WF Global Equity;LU0000000000;10;150,5;EUR;1505\n"
                               "Apple;US0378331005;5;200;USD;1000\nTotaal;;;;;2505\n")
    assert res["format"] == "table" and len(res["positions"]) == 2
    assert res["positions"][0]["value"] == 1505.0 and res["positions"][1]["value"] is None
    assert res["warnings"] == ["no EUR value for Apple"]


def test_parse_ibkr_statement():
    res = positions.parse_text(IBKR_CSV)
    assert res["format"] == "ibkr" and res["as_of"] == "2026-10-31" and res["total_value"] == 9667.1
    by = {p["symbol"]: p for p in res["positions"]}
    assert by["VWRA"]["value"] == pytest.approx(4816.0) and by["VWRA"]["isin"] == "IE00BK5BQT80"
    assert by["VWRA"]["name"].startswith("VANGUARD") and by["IWDA"]["value"] == 4000.0
    assert by[None]["asset_class"] == "cash" and by[None]["value"] == 250.5


def test_import_positions_sets_the_value(biz, tmp_path):  # noqa: F811
    f = tmp_path / "Portfolio.csv"
    f.write_text(DEGIRO_CSV, encoding="utf-8")
    assets.add_account(biz, "DEGIRO", "investment", today=TODAY)
    res = assets.import_positions(biz, "degiro", str(f), "2026-11-05", today=TODAY)
    assert res["value_updated"] and res["total_value"] == pytest.approx(73548.11) and res["format"] == "degiro"
    a = res["account_now"]
    assert a["value"] == pytest.approx(73548.11) and a["positions"]["count"] == 3
    classes = {p["name"][:8]: p["asset_class"] for p in assets.latest_positions(biz)}
    assert classes == {"CASH & C": "cash", "VANGUARD": "etf", "ISHARES ": "etf"}
    f2 = tmp_path / "lynx.csv"
    f2.write_text(IBKR_CSV, encoding="utf-8")
    assets.add_account(biz, "LYNX", "investment", today=TODAY)
    res = assets.import_positions(biz, "lynx", str(f2), today=TODAY)
    assert res["as_of"] == "2026-10-31" and res["account_now"]["value"] == 9667.1


def test_set_positions_without_eur_value_keeps_the_value(biz):  # noqa: F811
    assets.add_account(biz, "Ledger", "crypto", 5000.0, "2026-11-01", today=TODAY)
    res = assets.set_positions(biz, "ledger", [{"name": "Bitcoin", "symbol": "BTC", "quantity": 0.1, "price": 90000,
                                                 "currency": "USD"}], "2026-11-06", today=TODAY)
    assert not res["value_updated"] and "warning" in res and res["account_now"]["value"] == 5000.0
    res = assets.set_positions(biz, "ledger", [{"name": "Bitcoin", "symbol": "BTC", "quantity": 0.1, "value": 7800},
                                                {"name": "Ethereum", "quantity": 1, "price": 2500, "currency": "EUR"}],
                               "2026-11-06", today=TODAY)
    assert res["value_updated"] and res["account_now"]["value"] == 10300.0
    assert {p["asset_class"] for p in assets.latest_positions(biz)} == {"crypto"}


# --- dashboard and tools ---------------------------------------------------------------

def test_dashboard_shows_assets(biz, tmp_path):  # noqa: F811
    import re
    assets.add_account(biz, "DEGIRO", "investment", 23500.0, "2026-11-01", today=TODAY)
    assets.add_account(biz, "Woning", "property", 400000.0, today=TODAY)
    assets.set_positions(biz, "degiro", [{"name": "VWCE", "isin": "IE00BK5BQT80", "value": 23500.0}], "2026-11-01",
                         today=TODAY)
    d = dashboard.collect(biz, TODAY)
    inv = d["investments"]
    assert {a["name"] for a in inv["accounts"]} == {"DEGIRO", "Woning"}
    assert inv["valuation"]["market_value"] == 23500.0 and inv["positions"][0]["isin"] == "IE00BK5BQT80"
    assert d["net_worth"]["property"] == 400000.0
    assert inv["value_timeline"]["value"][-1] == 23500.0
    html = dashboard.render(d)
    assert not re.search(r"\b(?:BE|DE)\d{14,20}\b", html)  # no full IBANs, also not from the broker links


def test_tool_calls_on_a_database(biz, monkeypatch):  # noqa: F811
    import json
    from centen import server
    monkeypatch.setenv("CENTEN_TODAY", TODAY.isoformat())
    monkeypatch.setenv("CENTEN_DB", biz.execute("PRAGMA database_list").fetchone()[2])
    assert json.loads(server.add_asset_account("DEGIRO", "investment", value=21000.0))["gain"] == 1000.0
    assert json.loads(server.set_asset_value("degiro", 22000.0))["value"] == 22000.0
    assert json.loads(server.update_asset_account("degiro", contributions=19000.0,
                                                  contributions_date="2026-11-01"))["contributions"] == 19000.0
    assert json.loads(server.set_positions("degiro", [{"name": "VWCE", "value": 22000.0}]))["value_updated"]
    assert json.loads(server.list_assets())["totals"]["investments_value"] == 22000.0
    assert json.loads(server.financial_position())["net_worth"]["investments"] == 22000.0
    assert "error" in json.loads(server.set_asset_value("nothing", 1.0))
    assert json.loads(server.remove_asset_account("degiro"))["removed"] == "DEGIRO"


def test_tools_are_registered():
    import asyncio
    from centen import server
    names = {t.name for t in asyncio.run(server.server.list_tools())}
    assert {"list_assets", "add_asset_account", "set_asset_value", "update_asset_account", "set_positions",
            "import_positions", "remove_asset_account"} <= names
