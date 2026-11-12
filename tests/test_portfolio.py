"""Broker exports (brokers.py) and portfolio management (portfolio.py)."""
import json
from datetime import date
from pathlib import Path

import pytest

from centen import assets, brokers, portfolio
from test_assets import IBKR_CSV
from test_business import biz  # noqa: F401  (fixture)

FIX = Path(__file__).parent / "fixtures"
TODAY = date(2026, 11, 6)


def test_detect_formats():
    assert brokers.detect((FIX / "Transactions.csv").read_text()) == "degiro_transactions"
    assert brokers.detect((FIX / "Account.csv").read_text()) == "degiro_account"
    assert brokers.detect((FIX / "bitvavo.csv").read_text()) == "bitvavo"
    assert brokers.detect(IBKR_CSV) == "ibkr"
    assert brokers.detect("Product,Symbool/ISIN,Aantal,Slotkoers,Lokale waarde,,Waarde in EUR\n") == "positions"


def test_degiro_transactions():
    tx = brokers.parse_file(FIX / "Transactions.csv")["transactions"]
    buy, sell, usd = tx
    assert (buy["kind"], buy["quantity"], buy["amount_eur"], buy["fee_eur"]) == ("buy", 100, -12840.0, 3.0)
    assert (sell["kind"], sell["quantity"], sell["amount_eur"]) == ("sell", 40, 5400.0)
    assert usd["currency"] == "USD" and usd["amount_eur"] == -2400.55 and usd["fee_eur"] == 7.0  # costs + AutoFX


def test_degiro_account_keeps_tax_and_converts_dividends():
    tx = brokers.parse_file(FIX / "Account.csv")["transactions"]
    kinds = sorted(t["kind"] for t in tx)
    assert kinds == ["deposit", "deposit", "dividend", "dividend_tax", "fee", "tax", "withdrawal"]  # no trades, no FX
    div = next(t for t in tx if t["kind"] == "dividend")
    assert div["currency"] == "USD" and div["amount_eur"] == pytest.approx(12.5 / 1.085, abs=0.01)
    assert next(t for t in tx if t["kind"] == "tax")["amount_eur"] == -15.41  # TOB with an order id stays


def test_bitvavo():
    tx = brokers.parse_file(FIX / "bitvavo.csv")["transactions"]
    assert [t["kind"] for t in tx] == ["deposit", "buy", "sell", "withdrawal", "income", "transfer_out"]  # cancelled out
    assert tx[1]["amount_eur"] == -9000.0 and tx[1]["fee_eur"] == 22.5 and tx[1]["quantity"] == 0.1


def test_ibkr_statement_transactions():
    text = IBKR_CSV + (
        "Trades,Header,DataDiscriminator,Asset Category,Currency,Symbol,Date/Time,Quantity,T. Price,C. Price,Proceeds,"
        "Comm/Fee,Basis,Realized P/L,MTM P/L,Code\n"
        'Trades,Data,Order,Stocks,EUR,IWDA,"2026-03-05, 09:30:01",40,90,91,-3600,-1.25,3601.25,0,40,O\n'
        "Trades,SubTotal,,Stocks,EUR,IWDA,,40,,,-3600,-1.25,3601.25,0,40,\n"
        "Dividends,Header,Currency,Date,Description,Amount\n"
        "Dividends,Data,USD,2026-03-20,VWRA(IE00BK5BQT80) Cash Dividend USD 0.5 per Share (Ordinary Dividend),20\n"
        "Dividends,Data,Total,,,20\n"
        "Withholding Tax,Header,Currency,Date,Description,Amount,Code\n"
        "Withholding Tax,Data,USD,2026-03-20,VWRA(IE00BK5BQT80) Cash Dividend - US Tax,-3,\n"
        "Deposits & Withdrawals,Header,Currency,Settle Date,Description,Amount\n"
        "Deposits & Withdrawals,Data,EUR,2026-03-01,Electronic Fund Transfer,5000\n")
    res = brokers.parse_text(text)
    by = {t["kind"]: t for t in res["transactions"]}
    assert by["buy"]["amount_eur"] == -3600.0 and by["buy"]["fee_eur"] == 1.25 and by["buy"]["time"] == "09:30:01"
    assert by["dividend"]["isin"] == "IE00BK5BQT80" and by["dividend"]["amount_eur"] == pytest.approx(17.2)
    assert by["dividend_tax"]["amount_eur"] == pytest.approx(-2.58) and by["deposit"]["amount_eur"] == 5000.0
    assert res["positions"]["total_value"] == 9667.1


def test_import_and_portfolio(biz, tmp_path):  # noqa: F811
    assets.add_account(biz, "DEGIRO", "investment", today=TODAY)
    res = portfolio.import_files(biz, "degiro", [str(FIX / "Transactions.csv"), str(FIX / "Account.csv")], today=TODAY)
    assert [f["new"] for f in res["files"]] == [3, 7]
    again = portfolio.import_files(biz, "degiro", str(FIX), today=TODAY)  # whole folder, overlapping
    assert sum(f.get("new", 0) for f in again["files"]) == 0
    assert any("Bitvavo" in f.get("error", "") for f in again["files"])  # other broker refused
    p = portfolio.summary(biz, "degiro", today=TODAY)
    acc = p["accounts"][0]
    vw = next(h for h in acc["holdings"] if h["isin"] == "IE00BK5BQT80")
    assert vw["quantity"] == 60 and vw["avg_cost"] == 128.43 and vw["cost"] == pytest.approx(7705.8)
    assert vw["realised"] == pytest.approx(259.8) and vw["value"] == 8100.0 and vw["unrealised"] == pytest.approx(394.2)
    t = acc["totals"]
    assert t["tax"] == 15.41 and t["dividends"] == pytest.approx(11.52) and t["other_costs"] == 2.5
    assert t["deposits"] == 16000.0 and t["withdrawals"] == 500.0
    d = assets.describe(biz, "asset:degiro", TODAY)
    assert d["contributions_source"] == "broker export"  # the export goes back further than the bank data
    tx = portfolio.transactions(biz, "degiro", kind="dividend")
    assert tx["matches"] == 1 and tx["transactions"][0]["isin"] == "IE0031442068"


def test_crypto_moves_and_rewards(biz):  # noqa: F811
    assets.add_account(biz, "Bitvavo", "crypto", today=TODAY)
    portfolio.import_files(biz, "bitvavo", str(FIX / "bitvavo.csv"), today=TODAY)
    hs = {h["symbol"]: h for h in portfolio.summary(biz, "bitvavo", today=TODAY)["accounts"][0]["holdings"]}
    assert hs["BTC"]["quantity"] == pytest.approx(0.03) and hs["BTC"]["moved_out"] == pytest.approx(0.02)
    assert hs["BTC"]["cost"] == pytest.approx(2706.75)  # 0.02 left at average cost, no result
    assert hs["BTC"]["realised"] == pytest.approx(226.87)
    assert hs["ETH"]["cost"] == 30.0 and hs["ETH"]["price_source"].startswith("price when received")


def test_xirr():
    assert portfolio.xirr([(date(2025, 1, 1), -1000), (date(2026, 1, 1), 1100)]) == pytest.approx(0.1, abs=1e-3)
    assert portfolio.xirr([(date(2025, 1, 1), -1000)]) is None


def test_tools(biz, monkeypatch):  # noqa: F811
    from centen import server
    monkeypatch.setenv("CENTEN_TODAY", TODAY.isoformat())
    server.add_asset_account("Bitvavo", "crypto")
    out = json.loads(server.import_broker_export("bitvavo", [str(FIX / "bitvavo.csv")]))
    assert out["files"][0]["new"] == 6
    assert json.loads(server.portfolio())["accounts"][0]["account"] == "Bitvavo"
    assert json.loads(server.portfolio_transactions(kind="buy"))["matches"] == 1
    assert "error" in json.loads(server.import_broker_export("nothing", ["x.csv"]))


def test_units_and_expiry_dedupe(biz):  # noqa: F811
    assert brokers._units("25.000") == 25000 and brokers._units("-378") == -378 and brokers._units("0,5") == 0.5
    assets.add_account(biz, "DEGIRO", "investment", today=TODAY)
    tx = ("Datum,Tijd,Product,ISIN,Beurs,Uitvoeringsplaats,Aantal,Koers,,Lokale waarde,,Waarde EUR,Wisselkoers,"
          "AutoFX Kosten,Transactiekosten en/of kosten van derden EUR,Totaal EUR,Order ID\n"
          '28-08-2023,15:12,BUND 19/24,DE0001141802,FRA,FRAA,25.000,"96,3720",EUR,"-24.093,00",EUR,"-24.093,00",'
          '"0,0000","0,00","-3,00","-24.096,00",\n'
          '16-10-2024,07:32,BUND 19/24,DE0001141802,FRA,,-25.000,"0,0000",EUR,"0,00",EUR,"0,00","0,0000","0,00",,'
          '"0,00",\n')
    acc = ("Datum,Tijd,Valutadatum,Product,ISIN,Omschrijving,FX,Mutatie,,Saldo,,Order Id\n"
           '21-10-2024,07:32,18-10-2024,BUND 19/24,DE0001141802,Contante Verrekening Obligatie,,EUR,"25.000,00",EUR,'
           '"25.384,17",\n'
           '21-10-2024,07:32,16-10-2024,BUND 19/24,DE0001141802,VERLOPEN OBLIGATIE: Verkoop 25.000 @ 0 EUR,,EUR,'
           '"0,00",EUR,"384,17",\n'
           '28-08-2023,15:16,28-08-2023,BUND 19/24,DE0001141802,Transactiebelasting België,,EUR,"-28,91",EUR,'
           '"6.903,29",\n'
           '28-08-2023,15:16,28-08-2023,BUND 19/24,DE0001141802,DEGIRO Transactiekosten en/of kosten van derden,,'
           'EUR,"-3,00",EUR,"6.932,20",\n')
    import tempfile
    d = Path(tempfile.mkdtemp())
    (d / "t.csv").write_text(tx, encoding="utf-8")
    (d / "a.csv").write_text(acc, encoding="utf-8")
    portfolio.import_files(biz, "degiro", str(d), today=TODAY)
    a = portfolio.summary(biz, "degiro", today=TODAY)["accounts"][0]
    assert not a["warnings"] and not a["holdings"]
    assert a["years"]["2024"]["realised"] == pytest.approx(904.0)  # redeemed at 25.000, bought at 24.096
    assert a["totals"]["tax"] == 28.91 and a["totals"]["trade_costs"] == 3.0  # costs counted once


def test_wallet_round_trip_keeps_cost(biz):  # noqa: F811
    csv_ = ("Date,Type,Currency,Amount,Quote Currency,Quote Price,Received / Paid Currency,Received / Paid Amount,"
            "Fee currency,Fee amount,Status,Transaction ID,Address\n"
            "2021-01-01 10:00:00,Buy,BTC,1,EUR,30000,EUR,-30000,,,Completed,a,\n"
            "2023-03-14 20:38:16,Withdrawal,BTC,-1,EUR,,,,,,Completed,b,bc1q\n"
            "2024-02-29 15:15:01,Deposit,BTC,1,EUR,,,,,,Completed,c,\n"
            "2024-03-01 10:00:00,Sell,BTC,-0.5,EUR,60000,EUR,30000,,,Completed,d,\n")
    import tempfile
    f = Path(tempfile.mkdtemp()) / "bv.csv"
    f.write_text(csv_, encoding="utf-8")
    assets.add_account(biz, "Bitvavo", "crypto", today=TODAY)
    portfolio.import_files(biz, "bitvavo", str(f), today=TODAY)
    h = portfolio.summary(biz, "bitvavo", today=TODAY)["accounts"][0]["holdings"][0]
    assert h["cost"] == 15000.0 and h["realised"] == 15000.0 and h["cost_complete"] and "moved_out" not in h
