"""r23: time-weighted return, risk of the current mix, look-through and diversification."""
from datetime import date

import pytest

from centen import assets, history, risk

T = date(2026, 11, 13)


def test_twr_ignores_deposits():
    # 1000 → +10 % → deposit 1000 at the start of month 2 (counted half) → flat
    vals = {"A": {"2026-01": 1000.0, "2026-02": 1100.0, "2026-03": 2100.0}}
    flows = {"A": {"2026-03": 1000.0}}
    t = risk.twr(vals, flows)
    assert t["months"] == ["2026-02", "2026-03"]
    assert t["returns"][0] == pytest.approx(0.10) and t["returns"][1] == pytest.approx(0.0)
    assert t["total"] == pytest.approx(0.10)
    # a new account appearing without a recorded deposit is not counted as a return
    vals["B"] = {"2026-03": 5000.0}
    assert risk.twr(vals, flows)["total"] == pytest.approx(0.10)
    # an impossible jump (value entered late) is skipped, not a +300 % month
    vals["A"]["2026-04"] = 8400.0
    assert "2026-04" in risk.twr(vals, flows)["skipped"]


def test_stats_drawdown():
    s = risk._stats([0.1, -0.2, -0.1, 0.3])
    # 1.1 → 0.88 → 0.792 → 1.0296; peak 1.1, trough 0.792 → -28 %
    assert s["max_drawdown"] == pytest.approx(0.792 / 1.1 - 1, abs=1e-4)
    assert s["worst_month"] == -0.2 and s["best_month"] == 0.3 and s["annualised"] is None


HS = [
    {"account": "D", "name": "Vanguard FTSE All-World UCITS ETF", "key": "IE00BK5BQT80", "isin": "IE00BK5BQT80",
     "class": "etf", "region": "Wereld", "value": 60000.0},
    {"account": "D", "name": "iShares Core S&P 500", "key": "IE00B5BMR087", "isin": "IE00B5BMR087", "class": "etf",
     "region": "VS", "value": 20000.0},
    {"account": "L", "name": "NVIDIA Corp", "key": "US67066G1040", "isin": "US67066G1040", "class": "equity",
     "region": "VS", "value": 15000.0},
    {"account": "B", "name": "BTC", "key": "BTC", "isin": None, "class": "crypto", "region": "Crypto", "value": 5000.0},
]


def test_look_through_and_score(conn):
    lt = risk.look_through(conn, HS)
    reg = {r["bucket"]: r["weight"] for r in lt["regions"]}
    # 60 % × 62 % + 20 % + 15 % = 72,2 % US
    assert reg["VS"] == pytest.approx(0.722, abs=1e-3) and reg["Crypto"] == 0.05
    assert sum(r["weight"] for r in lt["regions"]) == pytest.approx(1, abs=1e-3)
    assert any(o["inside"] == "S&P 500" for o in lt["overlaps"])
    d = risk.diversification(HS, lt)
    texts = " ".join(p["text"] for p in d["penalties"])
    assert "NVIDIA" in texts and "VS" in texts and 0 < d["score"] < 100
    assert lt["unknown_sector_share"] == 0.15
    risk.set_sector(conn, "US67066G1040", "it")
    lt2 = risk.look_through(conn, HS)
    assert lt2["unknown_sector_share"] == 0 and {r["bucket"] for r in lt2["sectors"]} >= {"IT", "Crypto"}
    with pytest.raises(ValueError):
        risk.set_sector(conn, "X", "Ruimtevaart")


def test_holdings_risk(conn):
    history.ensure(conn)
    rows = []
    for i in range(40):
        y, m = 2023 + (i + 6) // 12, (i + 6) % 12 + 1
        d = f"{y}-{m:02d}-28"
        rows.append(("IE00BK5BQT80", d, 100 * 1.01 ** i, "EUR", 100 * 1.01 ** i, "1mo", "test"))
        rows.append(("BTC", d, 1000 * (1.2 if i % 2 else 0.85) ** 1 * (1 + i / 100), "EUR",
                     1000 * (1.2 if i % 2 else 0.85) * (1 + i / 100), "1mo", "test"))
    conn.executemany("INSERT INTO price_history(key, date, close, currency, close_eur, interval, source) VALUES "
                     "(?,?,?,?,?,?,?)", rows)
    conn.commit()
    hr = risk.holdings_risk(conn, [HS[0], HS[3]])
    assert hr["available"] and hr["months"] == 36 and hr["coverage"] == 1.0
    btc = next(h for h in hr["holdings"] if h["key"] == "BTC")
    vw = next(h for h in hr["holdings"] if h["key"] == "IE00BK5BQT80")
    assert btc["volatility"] > vw["volatility"] and btc["risk_share"] > 0.9  # 8 % of the money, nearly all the risk


def test_report_runs_on_demo(conn):
    from centen import demo, networth
    demo.generate_full(conn, T)
    networth.reconstruct(conn, T)
    r = risk.report(conn, T)
    tw = r["time_weighted"]
    # the demo's DEGIRO is only valued by estimate (value + deposits): no market months, so no TWR but a note
    assert "error" not in tw and tw["total"]["months"] == [] and tw["note"] and tw["left_out"] == ["DEGIRO"]
