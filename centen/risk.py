"""Return and risk of the portfolio (r23): what the money did, how bumpy the ride was, and what is really inside.

1. Time-weighted return (TWR) from the month-end values of the investment and crypto accounts and the money put in
   or taken out each month (Modified Dietz per month, chained). It is the return of the investments themselves,
   independent of when and how much you deposited, so it can be compared with a fund or index. Also: per account,
   annualised, volatility, maximum drawdown, best and worst month, and the same figures for the benchmark.
2. Risk of what you hold now: today's weights replayed over the monthly prices of the last years (volatility, maximum
   drawdown, worst month), per holding its volatility and share of the total risk, correlations.
3. Look-through: the regions and sectors inside the index funds (approximate index weights, see FUNDS), stocks by
   their own sector, and a diversification score with what pulls it down.

A planning aid, not advice: the look-through uses approximate index weights and month-end values.
"""
from __future__ import annotations

import json
import math
import re
from datetime import date

from . import analysis as AN
from . import assets as AS
from . import db
from .money import normalize_text

INVEST_TYPES = ("investment", "crypto")
META_SECTORS = "instrument_sectors"

SECTORS = ("IT", "Financiën", "Industrie", "Gezondheidszorg", "Cons. discretionair", "Communicatie", "Basisconsumptie",
           "Energie", "Materialen", "Nutsbedrijven", "Vastgoed")
_S = dict(zip(SECTORS, range(len(SECTORS))))


def _sec(*w):
    return dict(zip(SECTORS, w))


# Approximate index weights (2026). Regions: VS, Europa, Japan, Azië-Pacific (developed, ex-Japan), Overig ontwikkeld
# (Canada, Israël…), Opkomende markten.
FUNDS = [
    {"family": "FTSE All-World / MSCI ACWI", "isins": {"IE00BK5BQT80", "IE00B3RBWM25", "IE00B6R52259", "IE00B3YLTY66",
                                                       "IE00BFY0GT14x"},
     "rx": r"all[- ]world|all[- ]country|\bacwi\b|ftse all",
     "regions": {"VS": 62, "Europa": 14, "Japan": 5, "Azië-Pacific": 3, "Overig ontwikkeld": 4, "Opkomende markten": 12},
     "sectors": _sec(26, 17, 11, 9, 10, 8, 6, 4, 4, 3, 2), "contains": {"MSCI World", "S&P 500", "Nasdaq-100",
                                                                     "MSCI Europe / Stoxx 600", "MSCI EM"}},
    {"family": "MSCI World", "isins": {"IE00B4L5Y983", "IE00BFY0GT14", "IE00BJ0KDQ92", "LU0274208692", "IE00BK1PV551"},
     "rx": r"\bmsci world\b|\bcore msci world\b|\bworld\b(?!.*(ex|small))|developed world|\bswrd\b|\biwda\b",
     "regions": {"VS": 72, "Europa": 15, "Japan": 5, "Azië-Pacific": 3, "Overig ontwikkeld": 5},
     "sectors": _sec(27, 16, 11, 10, 10, 8, 6, 4, 3, 3, 2), "contains": {"S&P 500", "Nasdaq-100", "MSCI Europe / Stoxx 600"}},
    {"family": "MSCI EM", "isins": {"IE00BKM4GZ66", "IE00B4L5YC18", "IE00B3VVMM84", "IE00BTJRMP35"},
     "rx": r"emerging|\bem imi\b|\bmsci em\b",
     "regions": {"Opkomende markten": 100}, "sectors": _sec(25, 24, 7, 3, 12, 10, 4, 4, 6, 3, 2), "contains": set()},
    {"family": "S&P 500", "isins": {"IE00B5BMR087", "IE00B3XXRP09", "IE00BFMXXD54", "IE00B6YX5C33", "LU0496786574"},
     "rx": r"s ?& ?p ?500|\bsp ?500\b|\bcore s&p\b",
     "regions": {"VS": 100}, "sectors": _sec(34, 13, 9, 9, 10, 10, 5, 3, 2, 3, 2), "contains": {"Nasdaq-100"}},
    {"family": "Nasdaq-100", "isins": {"IE0032077012", "IE00B53SZB19", "IE00BMFKG444"},
     "rx": r"nasdaq[- ]?100|\bnasdaq\b",
     "regions": {"VS": 100}, "sectors": _sec(52, 1, 5, 5, 13, 16, 5, 1, 1, 1, 0), "contains": set()},
    {"family": "MSCI Europe / Stoxx 600", "isins": {"IE00B4K48X80", "LU0908500753", "DE0002635307", "IE00B1YZSC51"},
     "rx": r"stoxx ?600|msci europe|\beurope\b|stoxx europe",
     "regions": {"Europa": 100}, "sectors": _sec(7, 22, 18, 14, 8, 4, 9, 5, 6, 5, 2), "contains": set()},
]
_FOCUS = {"S&P 500": "de VS", "Nasdaq-100": "Amerikaanse tech", "MSCI World": "ontwikkelde markten (vooral de VS)",
          "MSCI Europe / Stoxx 600": "Europa", "MSCI EM": "opkomende markten"}
BOND_RX = r"\bbond|obligati|treasury|covered|aggregate|\bgovt\b|corp(orate)? bond"
GOLD_RX = r"\bgold\b|\bgoud\b|physical (gold|silver)|\betc\b"


def r2(x):
    return None if x is None else round(float(x), 2)


def r4(x):
    return None if x is None else round(float(x), 4)


# --- what is inside -------------------------------------------------------------------------------

def fund_of(name: str | None, isin: str | None) -> dict | None:
    for f in FUNDS:
        if isin and isin in f["isins"]:
            return f
    n = normalize_text(name or "")
    if re.search(BOND_RX, n):
        return None
    for f in FUNDS:
        if re.search(f["rx"], n):
            return f
    return None


def _sectors_meta(conn) -> dict:
    try:
        return json.loads(db.get_meta(conn, META_SECTORS) or "{}")
    except ValueError:
        return {}


def set_sector(conn, instrument: str, sector: str | None) -> dict:
    """The sector of a stock (or a fund the look-through doesn't know), e.g. 'IT'. None removes it."""
    key = instrument.strip().upper()
    if sector:
        m = {s.lower(): s for s in SECTORS + ("Obligaties", "Grondstoffen", "Gemengd")}
        if sector.lower() not in m:
            raise ValueError(f"Unknown sector {sector!r}. Use one of: {', '.join(m.values())}.")
        sector = m[sector.lower()]
    meta = _sectors_meta(conn)
    if sector:
        meta[key] = sector
    else:
        meta.pop(key, None)
    db.set_meta(conn, META_SECTORS, json.dumps(meta))
    conn.commit()
    return {"instrument": key, "sector": sector}


def look_through(conn, hs: list[dict]) -> dict:
    sec_meta = _sectors_meta(conn)
    regions, sectors, rows = {}, {}, []
    total = sum(h["value"] for h in hs) or 1.0

    def add(d, k, v):
        d[k] = d.get(k, 0.0) + v
    for h in hs:
        v = h["value"]
        cls = h["class"]
        n = normalize_text(h.get("name") or "")
        f = fund_of(h.get("name"), h.get("isin")) if cls not in ("crypto", "cash", "bond", "equity") else None
        own_sec = sec_meta.get((h.get("key") or "").upper())
        if cls == "crypto":
            add(regions, "Crypto", v); add(sectors, "Crypto", v); how = "crypto"  # noqa: E702
        elif cls == "cash":
            add(regions, "Cash", v); add(sectors, "Cash", v); how = "cash"  # noqa: E702
        elif cls == "bond" or re.search(BOND_RX, n):
            add(regions, h["region"] if h["region"] != "Overig" else "Obligaties", v)
            add(sectors, "Obligaties", v)
            how = "obligaties"
        elif re.search(GOLD_RX, n) and cls != "equity":
            add(regions, "Grondstoffen", v); add(sectors, "Grondstoffen", v); how = "grondstof"  # noqa: E702
        elif f:
            for k, w in f["regions"].items():
                add(regions, k, v * w / sum(f["regions"].values()))
            sw = sum(f["sectors"].values())
            for k, w in f["sectors"].items():
                if w:
                    add(sectors, k, v * w / sw)
            how = f"doorkijk {f['family']}"
        else:
            reg = h["region"]
            add(regions, {"Wereld": "Overig", "Wereld ex-VS": "Overig"}.get(reg, reg), v)
            add(sectors, own_sec or ("Onbekend (aandeel)" if cls == "equity" else "Onbekend (fonds)"), v)
            how = "eigen sector" if own_sec else ("aandeel" if cls == "equity" else "niet herkend")
        rows.append({"name": h.get("name"), "key": h.get("key"), "account": h["account"], "value": r2(v),
                     "weight": r4(v / total), "how": how, "family": f["family"] if f else None})

    def out(d):
        return [{"bucket": k, "value": r2(x), "weight": r4(x / total)} for k, x in sorted(d.items(), key=lambda kv: -kv[1])]
    # funds that largely hold the same thing
    fams = {}
    for r in rows:
        if r["family"]:
            fams[r["family"]] = fams.get(r["family"], 0.0) + (r["value"] or 0)
    overlaps = []
    for a in FUNDS:
        for b in FUNDS:
            if a["family"] in fams and b["family"] in fams and b["family"] in a["contains"]:
                overlaps.append({"wide": a["family"], "inside": b["family"], "value_inside": r2(fams[b["family"]]),
                                 "text": f"{b['family']} zit al in {a['family']}: dat fonds erbij is geen extra spreiding, "
                                         f"wel een grotere weging naar {_FOCUS.get(b['family'], b['family'])}."})
    unknown = sum(r["value"] or 0 for r in rows if r["how"] in ("aandeel", "niet herkend"))
    return {"regions": out(regions), "sectors": out(sectors), "holdings": rows, "overlaps": overlaps,
            "unknown_sector_share": r4(unknown / total)}


def diversification(hs: list[dict], lt: dict) -> dict:
    total = sum(h["value"] for h in hs) or 1.0
    w = sorted(((h["value"] / total, h) for h in hs), key=lambda x: -x[0])
    single = [(x, h) for x, h in w if h["class"] == "equity"]
    fund_w = sum(x for x, h in w if h["class"] in ("etf", "fund"))
    reg = {r["bucket"]: r["weight"] for r in lt["regions"]}
    sec = {r["bucket"]: r["weight"] for r in lt["sectors"] if not r["bucket"].startswith("Onbekend")}
    crypto = reg.get("Crypto", 0.0)
    top_region = max(((k, v) for k, v in reg.items() if k not in ("Crypto", "Cash", "Obligaties", "Grondstoffen")),
                     key=lambda kv: kv[1], default=(None, 0.0))
    eq_sec = {k: v for k, v in sec.items() if k in SECTORS}
    eq_tot = sum(eq_sec.values()) or 1.0
    top_sector = max(eq_sec.items(), key=lambda kv: kv[1], default=(None, 0.0))
    eff_sectors = 1 / sum((v / eq_tot) ** 2 for v in eq_sec.values()) if eq_sec else 0.0
    eff_positions = 1 / sum(x * x for x, _ in w) if w else 0.0
    pens, score = [], 100.0

    def pen(points, text):
        nonlocal score
        if points >= 1:
            score -= points
            pens.append({"points": round(points), "text": text})
    if single:
        x, h = single[0]
        pen(min(25, max(0, (x - 0.05) * 100 * 1.2)), f"Grootste aandeel {h.get('name')}: {x:.0%} van je portefeuille")
    pen(min(15, max(0, (crypto - 0.10) * 100 * 0.75)), f"Crypto {crypto:.0%} (zeer beweeglijk)")
    if top_region[0]:
        share = top_region[1] / max(1e-9, sum(v for k, v in reg.items() if k not in ("Crypto", "Cash", "Obligaties", "Grondstoffen")))
        pen(min(15, max(0, (share - 0.65) * 100 * 0.6)), f"{top_region[0]} is {share:.0%} van je aandelen (na doorkijk)")
    if top_sector[0]:
        s = top_sector[1] / eq_tot
        pen(min(15, max(0, (s - 0.30) * 100 * 0.75)), f"Sector {top_sector[0]} is {s:.0%} van je aandelen (na doorkijk)")
    if eq_sec and eff_sectors < 7:
        pen(min(10, (7 - eff_sectors) * 2.5), f"Effectief maar {eff_sectors:.1f} sectoren")
    if fund_w < 0.5 and len(w) < 15:
        pen(10, f"Weinig spreiding via fondsen ({fund_w:.0%}) en {len(w)} posities")
    if lt["unknown_sector_share"] > 0.15:
        pens.append({"points": 0, "text": f"{lt['unknown_sector_share']:.0%} zonder gekende sector: geef de sector van je "
                                          "aandelen in voor een betere score (set_instrument_sector)."})
    score = max(0.0, score)
    label = "goed gespreid" if score >= 80 else "redelijk gespreid" if score >= 60 else "geconcentreerd"
    return {"score": round(score), "label": label, "penalties": pens,
            "effective_positions": round(eff_positions, 1), "effective_sectors": round(eff_sectors, 1),
            "largest_position": {"name": w[0][1].get("name"), "weight": r4(w[0][0])} if w else None,
            "largest_stock": {"name": single[0][1].get("name"), "weight": r4(single[0][0])} if single else None,
            "top_region": {"name": top_region[0], "weight": r4(top_region[1])} if top_region[0] else None,
            "top_sector": {"name": top_sector[0], "weight": r4(top_sector[1])} if top_sector[0] else None,
            "funds_share": r4(fund_w), "crypto_share": r4(crypto)}


# --- statistics ------------------------------------------------------------------------------------

def _stats(rets: list[float]) -> dict:
    n = len(rets)
    if not n:
        return {"months": 0}
    idx, peak, mdd, trough_at, peak_at, cur_peak_at = 1.0, 1.0, 0.0, None, None, 0
    path = []
    for i, r in enumerate(rets):
        idx *= 1 + r
        path.append(idx)
        if idx > peak:
            peak, cur_peak_at = idx, i + 1
        dd = idx / peak - 1
        if dd < mdd:
            mdd, trough_at, peak_at = dd, i, cur_peak_at
    mean = sum(rets) / n
    sd = math.sqrt(sum((r - mean) ** 2 for r in rets) / (n - 1)) if n > 1 else None
    total = path[-1] - 1
    return {"months": n, "total": r4(total), "annualised": r4((1 + total) ** (12 / n) - 1) if n >= 12 else None,
            "volatility": r4(sd * math.sqrt(12)) if sd is not None else None, "max_drawdown": r4(mdd),
            "drawdown_peak_index": peak_at, "drawdown_trough_index": trough_at,
            "best_month": r4(max(rets)), "worst_month": r4(min(rets)),
            "positive_months": r4(sum(1 for r in rets if r > 0) / n)}


def _corr(a: list[float], b: list[float]) -> float | None:
    n = len(a)
    if n < 6:
        return None
    ma, mb = sum(a) / n, sum(b) / n
    sa = math.sqrt(sum((x - ma) ** 2 for x in a))
    sb = math.sqrt(sum((x - mb) ** 2 for x in b))
    if not sa or not sb:
        return None
    return sum((x - ma) * (y - mb) for x, y in zip(a, b)) / (sa * sb)


def _prev_month(m: str) -> str:
    y, mo = int(m[:4]), int(m[5:7])
    return f"{y - 1}-12" if mo == 1 else f"{y}-{mo - 1:02d}"


# --- 1. time-weighted return ---------------------------------------------------------------------

def _accounts(conn) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT id, name, account_type FROM accounts WHERE source='asset' AND "
                                          "account_type IN ('investment', 'crypto') ORDER BY name")]


def account_values(conn, today: date) -> dict[str, dict[str, float]]:
    """name -> {month: month-end value} from the net worth history (reconstructed months; months where the value is
    only an estimate at contributions are left out) plus today's value."""
    from . import networth
    accs = {a["name"]: a for a in _accounts(conn)}
    out = {n: {} for n in accs}
    for p in networth.timeline(conn)["points"]:
        m = p["date"][:7]
        skip = set(p.get("at_contributions") or []) | set(p.get("estimated_accounts") or [])
        for n, v in (p.get("by_account") or {}).items():
            if n in out and n not in skip and v is not None:
                out[n][m] = float(v)
    for n, a in accs.items():
        d = AS.describe(conn, a["id"], today)
        if d.get("value") is not None and not d.get("estimated"):
            out[n][today.isoformat()[:7]] = float(d["value"])
    return out


def account_flows(conn, today: date) -> dict[str, dict[str, float]]:
    out = {}
    for a in _accounts(conn):
        fl, _ = AS.contribution_flows(conn, a["id"])
        m = {}
        for d, amt, _ in fl:
            if d <= today.isoformat():
                m[d[:7]] = m.get(d[:7], 0.0) + amt
        out[a["name"]] = m
    return out


def twr(values: dict[str, dict[str, float]], flows: dict[str, dict[str, float]], accounts: list[str] | None = None) -> dict:
    """Month by month: the result of the accounts that have a value at both month-ends, over what was in them at the
    start plus half of what was added during the month (Modified Dietz), chained."""
    names = accounts or list(values)
    months = sorted({m for n in names for m in values.get(n, {})})
    rets, used, skipped = [], [], []
    for m in months:
        p = _prev_month(m)
        num = den = 0.0
        n_acc = 0
        for n in names:
            v = values.get(n, {})
            if p in v and m in v:
                f = flows.get(n, {}).get(m, 0.0)
                num += v[m] - v[p] - f
                den += v[p] + 0.5 * f
                n_acc += 1
        if not n_acc:
            continue
        if den <= 1.0:
            skipped.append(m)
            continue
        r = num / den
        if abs(r) > 0.8:  # a jump this big is a data gap (value entered late, transfer not seen), not a return
            skipped.append(m)
            continue
        rets.append(r)
        used.append(m)
    return {**_stats(rets), "months": used, "returns": [r4(r) for r in rets], "skipped": skipped}


def time_weighted(conn, today: date | None = None) -> dict:
    from . import history as H
    today = today or date.today()
    vals, fl = account_values(conn, today), account_flows(conn, today)
    accs = _accounts(conn)
    total = twr(vals, fl)
    per = []
    for a in accs:
        t = twr(vals, fl, [a["name"]])
        if t["months"]:
            per.append({"account": a["name"], "type": a["account_type"], "months": len(t["months"]),
                        **{k: t[k] for k in ("total", "annualised", "volatility", "max_drawdown", "worst_month",
                                             "best_month")}, "from": t["months"][0]})
    # the benchmark over the same months
    bench = AN.benchmark()
    series = H.monthly_series(conn, bench["key"])
    b_rets, b_months = [], []
    for m in total["months"]:
        p = _prev_month(m)
        if m in series and p in series and series[p]:
            b_rets.append(series[m] / series[p] - 1)
            b_months.append(m)
    own_aligned = [r for m, r in zip(total["months"], total["returns"]) if m in set(b_months)]
    out = {"total": {k: v for k, v in total.items()}, "accounts": per,
           "benchmark": {**_stats(b_rets), "name": bench["name"], "months": b_months, "returns": [r4(r) for r in b_rets]},
           "correlation_with_benchmark": r4(_corr(own_aligned, b_rets)) if len(own_aligned) == len(b_rets) else None}
    if out["correlation_with_benchmark"] is not None and b_rets:
        n = len(b_rets)
        mb, mo = sum(b_rets) / n, sum(own_aligned) / n
        vb = sum((x - mb) ** 2 for x in b_rets)
        out["beta"] = r4(sum((x - mo) * (y - mb) for x, y in zip(own_aligned, b_rets)) / vb) if vb else None
    # index lines for a chart (start = 100)
    line, i_own, i_b = [], 100.0, 100.0
    bmap = dict(zip(b_months, b_rets))
    for m, r in zip(total["months"], total["returns"]):
        i_own *= 1 + r
        if m in bmap:
            i_b *= 1 + bmap[m]
        line.append({"month": m, "own": round(i_own, 2), "benchmark": round(i_b, 2) if b_months else None})
    out["line"] = line
    if not total["months"]:
        out["note"] = ("Nog geen tijdgewogen rendement: dat vraagt maandwaarden uit koersen (brokerexport met posities "
                       "of een positie-overzicht). Rekeningen met enkel een ingegeven waarde tellen niet mee.")
    out["left_out"] = [a["name"] for a in accs if a["name"] not in {p["account"] for p in per}]
    return out


# --- 2. risk of what you hold now ------------------------------------------------------------------

def holdings_risk(conn, hs: list[dict], months: int = 36) -> dict:
    from . import history as H
    total = sum(h["value"] for h in hs) or 1.0
    by_key: dict[str, dict] = {}
    for h in hs:
        k = h.get("isin") or h.get("key")
        if not k:
            continue
        b = by_key.setdefault(k, {"key": k, "name": h.get("name"), "class": h["class"], "value": 0.0})
        b["value"] += h["value"]
    series = {k: H.monthly_series(conn, k) for k in by_key}
    all_months = sorted({m for s in series.values() for m in s})
    window = all_months[-(months + 1):]
    rets: dict[str, list[float | None]] = {}
    for k, s in series.items():
        rs = []
        for a, b in zip(window, window[1:]):
            rs.append(s[b] / s[a] - 1 if a in s and b in s and s[a] else None)
        if sum(r is not None for r in rs) >= max(6, len(rs) // 2):
            rets[k] = rs
    covered = sum(by_key[k]["value"] for k in rets)
    if not rets or len(window) < 7:
        return {"available": False, "note": "Te weinig koershistoriek: wordt aangevuld bij de volgende prijsupdate.",
                "coverage": r4(covered / total)}
    port = []
    for i in range(len(window) - 1):
        num = den = 0.0
        for k, rs in rets.items():
            if rs[i] is not None:
                num += by_key[k]["value"] * rs[i]
                den += by_key[k]["value"]
        port.append(num / den if den else 0.0)
    st = _stats(port)
    n = len(port)
    mp = sum(port) / n
    varp = sum((x - mp) ** 2 for x in port) / (n - 1) if n > 1 else 0.0
    rows = []
    for k, rs in rets.items():
        pairs = [(r, p) for r, p in zip(rs, port) if r is not None]
        a, b = [x for x, _ in pairs], [y for _, y in pairs]
        ma, mb = sum(a) / len(a), sum(b) / len(b)
        sd = math.sqrt(sum((x - ma) ** 2 for x in a) / (len(a) - 1)) if len(a) > 1 else None
        cov = sum((x - ma) * (y - mb) for x, y in zip(a, b)) / (len(a) - 1) if len(a) > 1 else 0.0
        w = by_key[k]["value"] / covered
        rows.append({"key": k, "name": by_key[k]["name"], "class": by_key[k]["class"], "weight": r4(w),
                     "volatility": r4(sd * math.sqrt(12)) if sd is not None else None,
                     "correlation": r4(_corr(a, b)), "risk_share": r4(w * cov / varp) if varp else None,
                     "worst_month": r4(min(a)) if a else None})
    rows.sort(key=lambda r: -(r["risk_share"] or 0))
    # correlations between the largest holdings
    top = sorted(rets, key=lambda k: -by_key[k]["value"])[:6]
    corr = []
    for i, a in enumerate(top):
        for b in top[i + 1:]:
            pairs = [(x, y) for x, y in zip(rets[a], rets[b]) if x is not None and y is not None]
            c = _corr([x for x, _ in pairs], [y for _, y in pairs])
            if c is not None:
                corr.append({"a": by_key[a]["name"], "b": by_key[b]["name"], "correlation": r4(c)})
    return {"available": True, "from": window[0], "to": window[-1], "months": n, "coverage": r4(covered / total),
            **{k: st[k] for k in ("annualised", "volatility", "max_drawdown", "worst_month", "best_month")},
            "holdings": rows, "correlations": sorted(corr, key=lambda c: -c["correlation"]),
            "note": "Je huidige verdeling teruggespeeld over de maandkoersen van de voorbije jaren: hoe beweeglijk deze "
                    "mix was, niet wat je effectief verdiende."}


def report(conn, today: date | None = None) -> dict:
    today = today or date.today()
    hs = AN.holdings(conn, today)
    out = {"as_of": today.isoformat()}
    try:
        out["time_weighted"] = time_weighted(conn, today)
    except Exception as e:  # noqa: BLE001
        out["time_weighted"] = {"error": str(e)[:200]}
    if hs:
        lt = look_through(conn, hs)
        out["look_through"] = lt
        out["diversification"] = diversification(hs, lt)
        try:
            out["holdings_risk"] = holdings_risk(conn, hs)
        except Exception as e:  # noqa: BLE001
            out["holdings_risk"] = {"available": False, "note": str(e)[:200]}
    out["notes"] = ("Tijdgewogen rendement (TWR): het rendement van de beleggingen zelf, los van wanneer en hoeveel je "
                    "stortte (per maand Modified Dietz, gekoppeld) — vergelijkbaar met een fonds of index. Je XIRR "
                    "(geldgewogen) houdt wél rekening met het moment van je stortingen. Doorkijk met benaderende "
                    "indexgewichten (2026). Maximale daling = grootste val van een top tot een later dal, op maandeinden.")
    return out
