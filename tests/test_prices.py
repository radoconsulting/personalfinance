"""Live prices (prices.py) with a simulated Yahoo Finance and Bitvavo."""
import json
from datetime import date, datetime

import httpx
import pytest

from centen import assets, portfolio, prices
from test_business import biz  # noqa: F401  (fixture)
from test_portfolio import FIX

TODAY = date(2026, 11, 6)


def _chart(symbol, price, ccy, prev):
    now = int(datetime.now().timestamp()) - 3600  # a fresh quote (the code drops listings older than 7 days)
    day = 86400
    return {"chart": {"result": [{"meta": {"regularMarketPrice": price, "currency": ccy, "symbol": symbol,
                                           "regularMarketTime": now},
                                  "timestamp": [now - 2 * day, now - day, now],
                                  "indicators": {"quote": [{"close": [prev, prev, price]}]}}]}}


def handler(request: httpx.Request):
    url = str(request.url)
    if "bitvavo" in url:
        return httpx.Response(200, json=[{"market": "BTC-EUR", "last": "70000", "open": "7.0E+4", "closeTimestamp":
                                          int(datetime(2026, 11, 6, 22).timestamp() * 1000)},
                                         {"market": "ADA-EUR", "last": "0.5", "open": "0.4"}])
    if "/search" in url:
        return httpx.Response(200, json={"quotes": [{"symbol": "VWRA.L", "exchange": "LSE"}]})
    sym = url.split("/chart/")[1].split("?")[0]
    table = {"IE00BK5BQT80.SG": (130.0, "EUR", 128.0), "EURUSD=X": (1.1, "USD", 1.1),
             "IE0031442068.SG": None, "VWRA.L": None}
    if sym.startswith("IE0031442068") or table.get(sym) is None:
        if sym == "IDUS.L":
            return httpx.Response(200, json=_chart(sym, 55.0, "USD", 50.0))
        return httpx.Response(404, json={"chart": {"result": None, "error": {"code": "Not Found"}}})
    return httpx.Response(200, json=_chart(sym, *table[sym]))


def test_refresh_values_holdings(biz):  # noqa: F811
    assets.add_account(biz, "DEGIRO", "investment", today=TODAY)
    portfolio.import_files(biz, "degiro", [str(FIX / "Transactions.csv")], today=TODAY)
    prices.set_symbol(biz, "IE0031442068", "IDUS.L")  # fixed by hand
    http = httpx.Client(transport=httpx.MockTransport(handler))
    res = prices.refresh(biz, http=http, today=TODAY)
    assert res["updated"] == 2 and not res["missing"]
    assert res["symbols"] == {"IE00BK5BQT80": "IE00BK5BQT80.SG", "IE0031442068": "IDUS.L"}
    hs = {h["isin"]: h for h in portfolio.summary(biz, "degiro", today=TODAY)["accounts"][0]["holdings"]}
    vw, sp = hs["IE00BK5BQT80"], hs["IE0031442068"]
    assert vw["value"] == 60 * 130.0 and vw["price_source"].startswith("live") and vw["day_change_pct"] == 0.0156
    assert sp["price_eur"] == pytest.approx(50.0) and sp["value"] == pytest.approx(2500.0)  # 55 USD / 1.1
    assert res["account_values"] == [{"account": "DEGIRO", "value": 10300.0}]
    assert assets.describe(biz, "asset:degiro", TODAY)["value"] == 10300.0
    # within 15 minutes nothing is fetched again
    assert "skipped" in prices.refresh(biz, http=http, today=TODAY, max_age_minutes=15)


def test_crypto_prices(biz):  # noqa: F811
    assets.add_account(biz, "Bitvavo", "crypto", today=TODAY)
    portfolio.import_files(biz, "bitvavo", str(FIX / "bitvavo.csv"), today=TODAY)
    http = httpx.Client(transport=httpx.MockTransport(handler))
    res = prices.refresh(biz, http=http, today=TODAY)
    assert res["symbols"]["BTC"] == "BTC-EUR" and {m["key"] for m in res["missing"]} == {"ETH"}
    btc = next(h for h in portfolio.summary(biz, "bitvavo", today=TODAY)["accounts"][0]["holdings"]
               if h["symbol"] == "BTC")
    assert btc["value"] == pytest.approx(0.03 * 70000) and btc["day_change_pct"] == 0.0


def test_tools_registered():
    import asyncio
    from centen import server
    names = {t.name for t in asyncio.run(server.server.list_tools())}
    assert {"refresh_prices", "set_price_symbol"} <= names


def test_previous_close_pairs_each_close_with_its_own_day():
    """Today's daily bar often has no close yet; dropping it before pairing shifted every close by a day."""
    from centen import prices

    class R:
        status_code = 200

        def json(self):
            return {"chart": {"result": [{"meta": {"regularMarketPrice": 68.23, "currency": "EUR", "gmtoffset": 3600,
                                                   "regularMarketTime": 1794484800},  # 12 Nov 2026 13:00 CET
                                          "timestamp": [1794297600, 1794384000, 1794470400],  # 10, 11, 12 Nov 08:00 UTC
                                          "indicators": {"quote": [{"close": [67.10, 68.36, None]}]}}]}}

    class Http:
        def get(self, *a, **k):
            return R()

    q = prices._chart(Http(), "IUSA.AS")
    assert q["prev"] == 68.36  # 11 Nov, not 10 Nov
