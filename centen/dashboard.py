"""Local HTML dashboard: one self-contained file built from the SQLite store (no network, no external scripts).

`centen dashboard` writes it to the data folder (next to the database) and opens it. It is rebuilt after every
sync and after every change to an investment / asset account. The investments section shows the accounts you added
(value, contributions, gain, positions) and money moved to brokers you haven't added yet.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path

from . import __version__, config
from . import analytics as A
from . import assets as AS

TEMPLATE = Path(__file__).with_name("dashboard_template.html")


def _strip(items, *keys):
    return [{k: v for k, v in i.items() if k not in keys} for i in items]


def _investment_timeline(conn, today: date) -> dict:
    kinds = ",".join(f"'{k}'" for k in sorted(AS.INVESTING_ASSET_TYPES))
    rows = conn.execute(
        "SELECT substr(t.booking_date,1,7) m, COALESCE(x.name, t.merchant), SUM(-t.amount) FROM transactions t "
        "LEFT JOIN accounts x ON x.id=t.asset_id WHERE t.status='booked' AND "
        f"((t.category='Savings & investments' AND t.asset_id IS NULL) OR x.account_type IN ({kinds})) "
        "GROUP BY 1,2 ORDER BY 1").fetchall()
    if not rows:
        return {"months": [], "series": []}
    first = date.fromisoformat(rows[0][0] + "-01")
    months = []
    d = first
    while d <= today:
        months.append(d.isoformat()[:7])
        d = A.add_months(d, 1)
    per = {}
    for m, merchant, amt in rows:
        per.setdefault(merchant or "Onbekend", {})[m] = amt
    series = []
    for name, by_month in per.items():
        run, vals = 0.0, []
        for m in months:
            run += by_month.get(m, 0.0)
            vals.append(round(run, 2))
        series.append({"name": name, "values": vals, "final": vals[-1]})
    series.sort(key=lambda s: -abs(s["final"]))
    return {"months": months, "series": series}


def collect(conn, today: date | None = None) -> dict:
    today = today or date.today()
    years = sorted({int(r[0]) for r in conn.execute("SELECT DISTINCT substr(booking_date,1,4) FROM transactions")
                    if r[0]})
    years = [y for y in years if y >= today.year - 2] or [today.year]
    periods = {}
    for y in years:
        f, t = date(y, 1, 1), min(date(y, 12, 31), today)
        pos = A.position(conn, f, t, today)
        pos["household"] = A.household_flows(conn, f, t)
        pos["label"] = str(y) if t == date(y, 12, 31) else f"{y} (t.e.m. {t.strftime('%d/%m')})"
        pos["cash"].pop("accounts", None)
        pos.pop("household_flows", None)
        periods[str(y)] = pos
    f12 = A.add_months(today, -12) + timedelta(days=1)
    pos = A.position(conn, f12, today, today)
    pos["household"] = A.household_flows(conn, f12, today)
    pos["label"] = "Laatste 12 maanden"
    pos["cash"].pop("accounts", None)
    pos.pop("household_flows", None)
    periods["12m"] = pos
    latest = periods[str(years[-1])]
    asum = AS.summary(conn, today)
    positions = AS.latest_positions(conn)
    from . import portfolio as PF
    pf = PF.summary(conn, today=today)
    pf = pf if pf["accounts"] else None
    months = max(13, (today.year - years[0]) * 12 + today.month)

    links = [{"bank": r["aspsp_name"], "status": r["status"], "valid_until": (r["valid_until"] or "")[:10],
              "days_left": (date.fromisoformat(r["valid_until"][:10]) - today).days if r["valid_until"] else None}
             for r in conn.execute("SELECT aspsp_name, status, valid_until FROM bank_links")]
    return {
        "meta": {"generated": datetime.now().strftime("%Y-%m-%d %H:%M"), "today": today.isoformat(),
                 "data_up_to": conn.execute("SELECT MAX(booking_date) FROM transactions").fetchone()[0],
                 "version": __version__, "default_period": str(years[-1])},
        "periods": periods,
        "period_order": [str(y) for y in reversed(years)] + ["12m"],
        "monthly": {p: A.cashflow(conn, months, today, perspective=p) for p in ("personal", "business", "total")},
        "recurring": _strip(A.recurring(conn, today), "account_id", "merchant_key"),
        "upcoming": A.upcoming(conn, 45, today),
        "alerts": A.unusual(conn, 60, today),
        "accounts": A.accounts_overview(conn),
        "links": links,
        "loans": latest["loans"],
        "net_worth": latest["net_worth"],
        "investments": {
            "by_destination": latest["investments"]["by_destination"],
            "net_deposited_total": latest["investments"]["net_deposited_total"],
            "timeline": _investment_timeline(conn, today),
            "accounts": asum["accounts"],      # added accounts incl. property, with value / contributions / gain
            "loans": asum["loans"],            # loans with the amount still owed
            "not_added": asum["not_added"],    # brokers seen in transfers, not added yet
            "totals": asum["totals"],
            "value_timeline": AS.timeline(conn, today),
            "positions": positions or None,    # latest holdings per account (from exports or entered)
            "portfolio": pf,                   # from broker exports: holdings with cost, results, years, XIRR
            "valuation": {"as_of": asum["totals"]["oldest_value"], "market_value": asum["totals"]["investments_value"],
                          "cost_basis": asum["totals"]["investments_contributions"],
                          "unrealised": asum["totals"]["gain_on_valued_accounts"]}
            if any(a.get("value_entered") for a in asum["accounts"]) else None,
        },
    }


def _safe(fn, *a, **k):
    """New sections never break the dashboard: an error becomes a note in that section."""
    try:
        return fn(*a, **k)
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}


def _extras(conn, today: date) -> dict:
    """r15 sections: net worth over time, Belgian tax view, portfolio analysis and the 'still to label' list."""
    from . import analysis, networth, review, tax
    from .categorize import CATEGORIES

    def tax_view():
        rep = tax.report(conn, today)
        cg = rep["capital_gains"]
        cg["latent"] = cg["latent"][:60]
        cg["disposals"] = cg["disposals"][-200:]
        return rep

    def review_view():
        out = {}
        for scope in ("personal", "business"):
            q = review.queue(conn, scope, 25, date_from=date(today.year - 1, 1, 1).isoformat(), today=today)
            for i in q["items"]:
                i.pop("transaction_ids", None)
            out[scope] = q
        return out

    from . import loans
    home = _safe(loans.overview, conn, today)
    return {**_r17(conn, today), "networth_history": _safe(networth.timeline, conn), "tax": _safe(tax_view),
            "home": home if isinstance(home, dict) and (home.get("loans") or home.get("property")) else None,
            "analysis": _safe(analysis.report, conn, today), "review": _safe(review_view),
            "categories": [c[0] for c in CATEGORIES if c[1] != "transfer" or c[0] == "Household transfers"]}


def _r17(conn, today: date) -> dict:
    """r17 sections: fixed costs, cash forecast, pension overview and the scenario planner."""
    from . import fixed, forecast, pension, planner

    def forecast_view():
        f = forecast.forecast(conn, today, 6)
        f["events_next_60_days"] = f["events_next_60_days"][:80]
        return f

    return {"fixed": _safe(fixed.report, conn, today), "forecast": _safe(forecast_view),
            "pension": _safe(pension.overview, conn, today), "planner": _safe(planner.plan, conn, today)}


def render(data: dict) -> str:
    payload = json.dumps(data, ensure_ascii=False, default=str).replace("</", "<\\/")
    return TEMPLATE.read_text(encoding="utf-8").replace("/*__DATA__*/{}", payload, 1)


def build(conn, path: Path | str | None = None, today: date | None = None) -> Path:
    path = Path(path) if path else config.home() / "dashboard.html"
    today = today or date.today()
    data = collect(conn, today)
    _daily(conn, today, data)
    data.update(_extras(conn, today))
    html = render(data)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(html, encoding="utf-8")
    tmp.replace(path)
    return path


def _daily(conn, today: date, data: dict) -> None:
    """Keep the net worth history up to date: today's figure as a snapshot and the month-ends rebuilt (cheap)."""
    from . import networth
    if today != date.today():
        return  # building for another day (tests, a look back): don't write snapshots
    _safe(networth.reconstruct, conn, today)
    pos = {"net_worth": data["net_worth"], "cash": data["periods"][data["meta"]["default_period"]]["cash"]}
    _safe(networth.snapshot, conn, today, pos)


def refresh(conn) -> Path | None:
    """Rebuild the dashboard after new data arrived; never lets a dashboard problem break a sync or import."""
    try:
        return build(conn)
    except Exception:  # noqa: BLE001
        return None
