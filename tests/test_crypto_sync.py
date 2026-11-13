"""r20: crypto kept up to date by itself (Bitvavo API, hardware wallets)."""
import json
import os
import tempfile
from datetime import date

import pytest

from centen import assets, crypto_sync as C, db, portfolio

T = date(2026, 11, 13)
ZPUB = "zpub6rFR7y4Q2AijBEqTUquhVz398htDFrtymD9xYYfG1m4wAcvPhXNfE3EfH1r1ADqtfSdVCToUG868RvUUkgDKf31mGDtKsAYz2oz2AGutZYs"
YPUB = "ypub6Ww3ibxVfGzLrAH1PNcjyAWenMTbbAosGNB6VvmSEgytSER9azLDWCxoJwW7Ke7icmizBMXrzBx9979FfaHxHcrArf3zbeJJJUZPf663zsP"
XPUB = "xpub6BosfCnifzxcFwrSzQiqu2DBVTshkCXacvNsWGYJVVhhawA7d4R5WSWGFNbi8Aw6ZRc1brxMyWMzG3DSSSSoekkudhUd9yLb6qx39T9nMdj"


@pytest.fixture()
def conn():
    os.environ["CENTEN_HOME"] = tempfile.mkdtemp()
    c = db.connect(os.environ["CENTEN_HOME"] + "/t.db")
    assets.add_account(c, "Bitvavo", "crypto", today=T)
    assets.add_account(c, "Ledger", "crypto", today=T)
    return c


def test_bip_vectors():
    assert C.derive(ZPUB, "p2wpkh", 0, 0, 2) == ["bc1qcr8te4kr609gcawutmrza0j4xv80jy8z306fyu",
                                                 "bc1qnjg0jd8228aq7egyzacy8cys3knf9xvrerkf9g"]
    assert C.derive(ZPUB, "p2wpkh", 1, 0, 1) == ["bc1q8c6fshw2dlwun7ekn9qwf37cu2rn755upcp6el"]
    assert C.derive(YPUB, "p2sh-p2wpkh", 0, 0, 1) == ["37VucYSaXLCAsxYyAPfbSi9eh4iEcbShgf"]
    assert C.derive(XPUB, "p2pkh", 0, 0, 1) == ["1LqBGSKuX5yYUonjxT5qGfpUsXKYYWeabA"]
    assert C._ripemd160_py(b"abc").hex() == "8eb208f7e05d987a9b044a8e98c6b087f15a0bfc"
    with pytest.raises(ValueError):
        C._decode_xkey("xprv9s21ZrQH143K3QTDL4LXw2F7HEK3wJUD2nW2nRk4stbPy6cq3jPPqjiChkVvvNKmPGJxWUtg6LnF5kejMRNNU3TGtRBeJgk33yuGBxrMPHi")


class FakeHTTP:
    """mempool.space: the first two receive addresses of the test zpub hold coins; ETH node: 1,5 ETH."""
    def __init__(self):
        self.funded = {"bc1qcr8te4kr609gcawutmrza0j4xv80jy8z306fyu": 50_000_000,
                       "bc1qnjg0jd8228aq7egyzacy8cys3knf9xvrerkf9g": 25_000_000}

    class R:
        def __init__(self, code, data):
            self.status_code, self._d, self.text = code, data, json.dumps(data)

        def json(self):
            return self._d

    def get(self, url, **kw):
        a = url.rsplit("/", 1)[-1]
        sats = self.funded.get(a, 0)
        return self.R(200, {"chain_stats": {"funded_txo_sum": sats, "spent_txo_sum": 0, "tx_count": 1 if sats else 0},
                            "mempool_stats": {}})

    def post(self, url, json=None, **kw):
        return self.R(200, {"jsonrpc": "2.0", "id": 1, "result": hex(1_500_000_000_000_000_000)})


def test_wallets(conn):
    from centen import prices
    prices.ensure(conn)
    conn.execute("INSERT INTO prices(key, date, symbol, price, currency, price_eur, source, fetched_at) "
                 "VALUES ('BTC', '2026-11-13', 'BTC-EUR', 80000, 'EUR', 80000, 'bitvavo', '2026-11-13T08:00')")
    conn.execute("INSERT INTO prices(key, date, symbol, price, currency, price_eur, source, fetched_at) "
                 "VALUES ('ETH', '2026-11-13', 'ETH-EUR', 3000, 'EUR', 3000, 'bitvavo', '2026-11-13T08:00')")
    conn.commit()
    C.set_wallet(conn, "Ledger", "BTC", xpub=ZPUB)
    C.set_wallet(conn, "Ledger", "ETH", addresses=["0xde0B295669a9FD93d5F28D9Ec85E40f4cb697BAe"])
    with pytest.raises(ValueError):
        C.set_wallet(conn, "Ledger", "ETH", addresses=["bc1qcr8te4kr609gcawutmrza0j4xv80jy8z306fyu"])
    res = C.sync_wallets(conn, FakeHTTP(), T, pause=0)
    got = {w["coin"]: w for w in res["wallets"]}
    assert got["BTC"]["quantity"] == 0.75 and got["BTC"]["value"] == 60000
    assert got["ETH"]["quantity"] == 1.5 and got["ETH"]["value"] == 4500
    acc = assets.describe(conn, assets.find(conn, "Ledger")["id"], T)
    assert acc["value"] == 64500
    assert "secret" not in json.dumps(C.status(conn))


class FakeBitvavo:
    def __init__(self, items, balance):
        self.items, self.balance = items, balance

    def get(self, path, params=None):
        if path == "/v2/account/history":
            return {"items": self.items, "currentPage": 1, "totalPages": 1, "maxItems": 100}
        if path == "/v2/balance":
            return self.balance
        raise ValueError("Bitvavo 404")


def test_bitvavo_history(conn):
    items = [
        {"transactionId": "t1", "executedAt": "2026-11-01T09:00:00.000Z", "type": "deposit", "receivedCurrency": "EUR",
         "receivedAmount": "1000", "feesCurrency": "EUR", "feesAmount": "0"},
        {"transactionId": "t2", "executedAt": "2026-11-02T09:00:00.000Z", "type": "buy", "priceCurrency": "EUR",
         "priceAmount": "80000", "sentCurrency": "EUR", "sentAmount": "800", "receivedCurrency": "BTC",
         "receivedAmount": "0.01", "feesCurrency": "EUR", "feesAmount": "2"},
        {"transactionId": "t3", "executedAt": "2026-11-03T09:00:00.000Z", "type": "staking", "priceCurrency": "EUR",
         "priceAmount": "3000", "receivedCurrency": "ETH", "receivedAmount": "0.001"},
        {"transactionId": "t4", "executedAt": "2026-11-04T09:00:00.000Z", "type": "withdrawal_cancelled",
         "sentCurrency": "BTC", "sentAmount": "0.01"},
    ]
    fb = FakeBitvavo(items, [{"symbol": "BTC", "available": "0.01", "inOrder": "0"},
                             {"symbol": "ETH", "available": "0.001", "inOrder": "0"}])
    os.environ["CENTEN_BITVAVO_KEY"], os.environ["CENTEN_BITVAVO_SECRET"] = "k" * 30, "s" * 40
    try:
        r = C.sync_bitvavo(conn, client=fb)
        assert r["new"] == 3 and r["differences"] == []
        assert C.sync_bitvavo(conn, client=fb)["new"] == 0  # nothing twice
        pos = {p["symbol"]: p["quantity"] for p in portfolio.compute(conn, assets.find(conn, "Bitvavo")["id"])
               ["positions"].values()}
        assert pos["BTC"] == pytest.approx(0.01) and pos["ETH"] == pytest.approx(0.001)
        fb.balance = [{"symbol": "BTC", "available": "0.02", "inOrder": "0"}]
        d = C.sync_bitvavo(conn, client=fb)["differences"]
        assert {x["coin"] for x in d} == {"BTC", "ETH"}
    finally:
        os.environ.pop("CENTEN_BITVAVO_KEY"), os.environ.pop("CENTEN_BITVAVO_SECRET")


def test_signature():
    b = C.Bitvavo("key", "secret", http=object())
    # HMAC-SHA256 over timestamp + method + path + body, hex
    import hashlib, hmac
    assert b.sign(1, "GET", "/v2/balance") == hmac.new(b"secret", b"1GET/v2/balance", hashlib.sha256).hexdigest()
