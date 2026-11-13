"""Pension overview (r17): the four Belgian pension pillars side by side, what each is worth today, what it may be
worth at your pension date, and what that means per month next to your spending today.

- Pillar 1, the legal pension: centen can't fetch it; enter the estimate from mypension.be (`set_pension_profile`).
- Pillar 2, supplementary pension through work: IPT / groepsverzekering / VAPZ / POZ, i.e. the pension accounts
  added with `add_asset_account(asset_type="pension")`. Yearly premium from the payments seen.
- Pillar 3, pensioensparen and langetermijnsparen: pension accounts whose name says so; yearly deposits are checked
  against the 2026 maxima.
- Pillar 4, your own savings and investments (brokers, crypto, tak 21/23), counted as capital you can live from.

Projections use simple, editable assumptions (returns, inflation, tax on payout) and are shown in today's euros.
They are a planning aid, not a pension calculation: the pension fiche of the insurer and mypension.be are leading.
"""
from __future__ import annotations

import json
import re
from datetime import date, timedelta

from . import analytics as A
from . import assets as AS
from . import db

META_KEY = "pension_profile"
DEFAULTS = {
    "birth_date": None,
    "retirement_age": 67,             # legal pension age from 2030
    "stop_work_age": None,            # stop working (and saving) earlier than the pension; None = at the pension
    "legal_pension_monthly": None,    # estimate from mypension.be, gross per month, today's euros
    "partner_legal_pension_monthly": None,
    "return_pension": 0.025,          # pillars 2 and 3, nominal per year
    "return_investments": 0.05,       # pillar 4, nominal per year
    "inflation": 0.02,
    "payout_tax": 0.165,              # rough total tax and contributions on a 2nd pillar capital at pension age
    "withdrawal_rate": 0.035,         # pillar 4: what you can take out per year
    "live_until": 90,
    "one_off_threshold": 10000.0,     # a single expense this big counts as one-off (unless it came a year earlier too)
    "one_off_exclude": [],            # transaction ids the user marked as one-off
    "one_off_include": [],            # transaction ids the user wants counted after all
    "spending_monthly": None,         # override of the spending profile (per month)
    "investing_yearly": None,         # override of the yearly new money into your own investments (pillar 4)
    "investing_years": 5,             # window for the historical pattern
    "accounts": {},                   # name -> {"pillar": 2|3|4, "expected_at_retirement": float, "premium_yearly": float}
}
# 2026 figures (income year 2026, assessment year 2027).
PENSION_SAVING_MAX = {"30%": 1050.0, "25%": 1350.0}
LONG_TERM_SAVING_MAX = 2450.0

_P2 = re.compile(r"\bipt\b|groepsverzeker|group insurance|vapz|plci|\bpoz\b|pensioentoezegging|bedrijfsleider|"
                 r"\bric\b|riziv|inami|pension libre|engagement individuel")
_P3 = re.compile(r"pensioenspar|epargne.pension|pension savings|langetermijnspar|long.term saving")


def r2(x):
    return None if x is None else round(float(x), 2)


def _e(v: float) -> str:
    return "€ " + f"{v:,.0f}".replace(",", ".")


def profile(conn) -> dict:
    raw = db.get_meta(conn, META_KEY)
    p = dict(DEFAULTS)
    if raw:
        try:
            p.update(json.loads(raw))
        except ValueError:
            pass
    p["accounts"] = dict(p.get("accounts") or {})
    return p


def set_profile(conn, reset: list[str] | None = None, **changes) -> dict:
    p = profile(conn)
    accounts = changes.pop("accounts", None) or {}
    for k in reset or []:
        if k not in DEFAULTS or k == "accounts":
            raise ValueError(f"Can't reset '{k}'.")
        p[k] = json.loads(json.dumps(DEFAULTS[k]))
    for k, v in changes.items():
        if v is None:
            continue
        if k not in DEFAULTS:
            raise ValueError(f"Unknown setting '{k}'.")
        if k == "birth_date":
            date.fromisoformat(v)
        elif k in ("return_pension", "return_investments", "inflation", "payout_tax", "withdrawal_rate"):
            v = float(v)
            if v > 1:  # accept 3 for 3 %
                v = v / 100
            if not -0.5 < v < 0.5:
                raise ValueError(f"{k} looks wrong: {v}")
        elif k == "stop_work_age":
            v = int(v)
            if not 25 <= v <= 110:
                raise ValueError(f"stop_work_age looks wrong: {v}")
        elif k in ("retirement_age", "live_until"):
            v = int(v)
            if not 50 <= v <= 110:
                raise ValueError(f"{k} looks wrong: {v}")
        elif k == "investing_years":
            v = int(v)
            if not 1 <= v <= 15:
                raise ValueError("investing_years must be 1 to 15")
        elif k in ("one_off_exclude", "one_off_include"):
            raise ValueError(f"Use mark_one_off for {k}.")
        else:
            v = float(v)
        p[k] = v
    for name, a in accounts.items():
        acc = AS.find(conn, name)
        cur = dict(p["accounts"].get(acc["name"]) or {})
        for k, v in (a or {}).items():
            if k not in ("pillar", "expected_at_retirement", "premium_yearly"):
                raise ValueError(f"Unknown account setting '{k}'.")
            if v is None:
                cur.pop(k, None)
            else:
                cur[k] = int(v) if k == "pillar" else float(v)
                if k == "pillar" and cur[k] not in (2, 3, 4):
                    raise ValueError("pillar must be 2, 3 or 4")
        p["accounts"][acc["name"]] = cur
    db.set_meta(conn, META_KEY, json.dumps(p))
    conn.commit()
    return p


def pillar_of(acc: dict, settings: dict) -> int:
    if settings.get("pillar"):
        return int(settings["pillar"])
    text = f"{acc['name']} {acc.get('provider') or ''}".lower()
    if acc["type"] == "pension":
        return 3 if _P3.search(text) and not _P2.search(text) else 2
    return 4


def _fv(value: float, premium: float, r: float, years: float, paying: float | None = None) -> float:
    """Value after `years` with a yearly premium paid during the first `paying` years (default: all of them)."""
    if years <= 0:
        return value
    n = years if paying is None else max(0.0, min(paying, years))
    g = (1 + r) ** n
    fv = value * g + (premium * (g - 1) / r if r else premium * n)
    return fv * (1 + r) ** (years - n)


def _premiums(conn, acc_name: str, today: date) -> tuple[float, float]:
    """Money into the account in the last 12 months and in this calendar year."""
    acc = AS.find(conn, acc_name)
    flows, _ = AS.contribution_flows(conn, acc["id"])
    y0 = today - timedelta(days=365)
    last12 = sum(f[1] for f in flows if date.fromisoformat(f[0][:10]) > y0)
    ytd = sum(f[1] for f in flows if f[0][:4] == str(today.year))
    return max(0.0, last12), max(0.0, ytd)


KEEP_CATEGORIES = {"Loans & mortgage"}  # never a one-off: the instalments are part of the spending profile


def _comes_back(conn, r) -> bool:
    """A big payment to the same merchant for about the same amount 11 to 13 months earlier: a yearly bill."""
    if not r["merchant_key"] or r["merchant_key"] == "unknown":
        return False
    d = date.fromisoformat(r["booking_date"][:10])
    amt = -r["amount"]
    return conn.execute("SELECT 1 FROM transactions WHERE merchant_key=? AND kind='expense' AND booking_date BETWEEN ? "
                        "AND ? AND -amount BETWEEN ? AND ? LIMIT 1",
                        (r["merchant_key"], (d - timedelta(days=400)).isoformat(),
                         (d - timedelta(days=330)).isoformat(), amt * 0.7, amt * 1.3)).fetchone() is not None


def one_offs(conn, start: date, end: date, p: dict | None = None) -> list[dict]:
    """Personal expenses between start and end that don't belong in the spending profile: single payments of at
    least the threshold that don't come back yearly, and large amounts coming back in a spending category (selling
    the old car), plus what the user marked; minus what the user kept. amount > 0 = money out, < 0 = money in."""
    p = p or profile(conn)
    excl, incl = set(p.get("one_off_exclude") or []), set(p.get("one_off_include") or [])
    thr = float(p.get("one_off_threshold") or DEFAULTS["one_off_threshold"])
    out = []
    for r in A.fetch(conn, start, end, kinds=["expense"], perspective="personal", include_pending=False):
        if r["id"] in incl:
            continue
        reason = None
        if r["id"] in excl:
            reason = "manual"
        elif -r["amount"] >= thr and r["category"] not in KEEP_CATEGORIES and not _comes_back(conn, r):
            reason = "auto"
        elif r["amount"] >= thr and r["category"] not in KEEP_CATEGORIES:
            reason = "auto"  # money back in a spending category (the sale of a car): one-off too
        if reason:
            out.append({"id": r["id"], "date": r["booking_date"], "merchant": r["merchant"], "category": r["category"],
                        "amount": r2(-r["amount"]), "reason": reason})
    return sorted(out, key=lambda x: -x["amount"])


def spending_profile(conn, today: date | None = None, p: dict | None = None) -> dict:
    """Personal spending per month over the last 12 months without one-offs (or the user's own figure)."""
    today = today or date.today()
    p = p or profile(conn)
    start = today - timedelta(days=365)
    raw = A.breakdown(conn, start, today, perspective="personal")["total"] or 0.0
    offs = one_offs(conn, start, today, p)
    left = raw - sum(o["amount"] for o in offs)
    auto = left / 12
    override = p.get("spending_monthly")
    return {"monthly": r2(override if override is not None else auto), "monthly_from_history": r2(auto),
            "monthly_raw": r2(raw / 12), "override": override is not None, "one_offs": offs,
            "one_offs_total": r2(sum(o["amount"] for o in offs)),
            "threshold": float(p.get("one_off_threshold") or DEFAULTS["one_off_threshold"]),
            "from": start.isoformat(), "to": today.isoformat()}


def mark_one_off(conn, transaction_ids: list[str], one_off: bool | None = True) -> dict:
    """one_off True = leave out of the spending profile, False = count it after all, None = back to automatic."""
    p = profile(conn)
    excl, incl = set(p["one_off_exclude"] or []), set(p["one_off_include"] or [])
    found = []
    for tid in transaction_ids:
        row = conn.execute("SELECT id, booking_date, merchant, amount FROM transactions WHERE id=?", (tid,)).fetchone()
        if row is None:
            raise ValueError(f"No transaction with id {tid} (search_transactions shows the ids).")
        excl.discard(tid)
        incl.discard(tid)
        if one_off is True:
            excl.add(tid)
        elif one_off is False:
            incl.add(tid)
        found.append({"id": tid, "date": row["booking_date"], "merchant": row["merchant"], "amount": row["amount"]})
    p["one_off_exclude"], p["one_off_include"] = sorted(excl), sorted(incl)
    db.set_meta(conn, META_KEY, json.dumps(p))
    conn.commit()
    return {"updated": found, "one_off": one_off}


def investing_pattern(conn, today: date, p: dict, accounts: list[dict]) -> dict:
    """New money into your own investments (pillar 4) per year, and the yearly average over the window."""
    years = int(p.get("investing_years") or 5)
    start = today - timedelta(days=round(365.25 * years))
    per_year, flows_all = {}, []
    for a in accounts:
        flows, _ = AS.contribution_flows(conn, a["id"])
        for d, amt, _payer in flows:
            per_year[d[:4]] = per_year.get(d[:4], 0.0) + amt
            flows_all.append((d[:10], amt))
    window = [f for f in flows_all if date.fromisoformat(f[0]) > start]
    first = min((date.fromisoformat(f[0]) for f in flows_all), default=None)
    span = years if first is None or first <= start else max(1.0, (today - first).days / 365.25)
    avg = max(0.0, sum(f[1] for f in window) / span) if flows_all else 0.0
    override = p.get("investing_yearly")
    return {"yearly": r2(override if override is not None else avg), "yearly_from_history": r2(avg),
            "override": override is not None, "window_years": round(span, 1),
            "per_year": [{"year": y, "net": r2(v)} for y, v in sorted(per_year.items())]}


def overview(conn, today: date | None = None) -> dict:
    today = today or date.today()
    p = profile(conn)
    needs = []
    if not p["birth_date"]:
        needs.append("geboortedatum")
    if p["legal_pension_monthly"] is None:
        needs.append("wettelijk pensioen per maand (raming van mypension.be)")
    ret_date = None
    years = None
    if p["birth_date"]:
        b = date.fromisoformat(p["birth_date"])
        ret_date = A.add_months(date(b.year + p["retirement_age"], b.month, 1), 1)  # first day of the month after
        years = max(0.0, (ret_date - today).days / 365.25)
    infl = (1 + p["inflation"]) ** (years or 0)
    # Premiums and new investments only while you work: until stop_work_age (default the pension date).
    stop_age = p.get("stop_work_age") or p["retirement_age"]
    paying = None
    if years is not None:
        age_now = (today - date.fromisoformat(p["birth_date"])).days / 365.25
        paying = max(0.0, min(years, stop_age - age_now))
    early = years is not None and paying < years - 0.05

    pillars = {2: [], 3: [], 4: []}
    p4_accounts = []
    for a in AS.list_accounts(conn, today):
        if a["type"] not in ("pension", "insurance", "investment", "crypto"):
            continue
        if a["scope"] != "personal" and a["type"] != "pension":
            continue  # company investments are the company's, not your pension
        s = p["accounts"].get(a["name"]) or {}
        pil = pillar_of(a, s)
        if pil == 4:
            p4_accounts.append(a)
        value = AS.counted_value(a)
        last12, ytd = _premiums(conn, a["name"], today)
        premium = s.get("premium_yearly", last12 if pil != 4 else 0.0)
        r = p["return_pension"] if pil in (2, 3) else p["return_investments"]
        proj = _fv(value, premium, r, years, paying) if years is not None else None
        exp = s.get("expected_at_retirement") if not early else None  # the fiche assumes premiums until the pension
        row = {"name": a["name"], "provider": a.get("provider"), "type": a["type"], "pillar": pil, "value": r2(value),
               "value_date": (a.get("value_entered") or {}).get("date"), "contributions": a.get("contributions"),
               "premium_last_12m": r2(last12), "premium_this_year": r2(ytd), "premium_assumed": r2(premium),
               "paid_by": sorted((a.get("contributions_by_payer") or {a["scope"]: 1}).keys()),
               "return_assumed": r, "projected_nominal": r2(proj),
               "projected_today_eur": r2(proj / infl) if proj is not None else None,
               "expected_at_retirement": r2(exp), "expected_today_eur": r2(exp / infl) if exp and years is not None else None,
               "stale": a.get("stale", False)}
        pillars[pil].append(row)

    inv = investing_pattern(conn, today, p, [{"id": AS.find(conn, a["name"])["id"]} for a in p4_accounts])
    if inv["yearly"] and years is not None and paying > 0:
        proj = _fv(0.0, inv["yearly"], p["return_investments"], years, paying)
        pillars[4].append({"name": "Nieuwe inleg tot je pensioen" if not early else f"Nieuwe inleg tot je {stop_age} jaar", "provider": None, "type": "contributions", "pillar": 4,
                           "value": 0.0, "value_date": None, "contributions": None, "premium_last_12m": None,
                           "premium_this_year": None, "premium_assumed": inv["yearly"], "paid_by": ["personal"],
                           "return_assumed": p["return_investments"], "projected_nominal": r2(proj),
                           "projected_today_eur": r2(proj / infl), "expected_at_retirement": None,
                           "expected_today_eur": None, "stale": False})

    sp = spending_profile(conn, today, p)
    spend_m = sp["monthly"] or None
    if early and spend_m:
        # Between stopping and the pension you live from your own capital (pillar 4).
        gap = years - paying
        rr = (1 + p["return_investments"]) / (1 + p["inflation"]) - 1
        used = spend_m * 12 * (((1 + rr) ** gap - 1) / rr if rr else gap)
        pillars[4].append({"name": f"Leven van je vermogen van {stop_age} tot {p['retirement_age']} jaar", "provider": None,
                           "type": "drawdown", "pillar": 4, "value": 0.0, "value_date": None, "contributions": None,
                           "premium_last_12m": None, "premium_this_year": None, "premium_assumed": r2(-spend_m * 12),
                           "paid_by": ["personal"], "return_assumed": p["return_investments"],
                           "projected_nominal": r2(-used * infl), "projected_today_eur": r2(-used),
                           "expected_at_retirement": None, "expected_today_eur": None, "stale": False})

    def cap(rows, key_nom, key_real):
        nom = sum((x["expected_at_retirement"] or x[key_nom] or 0) for x in rows)
        real = sum((x["expected_today_eur"] or x[key_real] or 0) for x in rows)
        return r2(nom), r2(real)

    payout_years = max(1, p["live_until"] - p["retirement_age"])
    out_p = []
    monthly_income = 0.0
    for pil, rows in pillars.items():
        nom, real = cap(rows, "projected_nominal", "projected_today_eur")
        value = r2(sum(x["value"] or 0 for x in rows))
        if pil in (2, 3):
            after_tax = (real or 0) * (1 - (p["payout_tax"] if pil == 2 else 0.08))
            per_month = after_tax / payout_years / 12 if years is not None else None
            method = (f"kapitaal na ±{p['payout_tax'] * 100:.1f} % belasting" if pil == 2 else
                      "kapitaal na 8 % belasting op 60 jaar") + f", gespreid over {payout_years} jaar"
        else:
            after_tax = real
            per_month = max(0.0, real or 0) * p["withdrawal_rate"] / 12 if years is not None else None
            method = f"{p['withdrawal_rate'] * 100:.1f} % van het kapitaal per jaar opnemen (het kapitaal blijft)"
        monthly_income += per_month or 0
        out_p.append({"pillar": pil, "label": {2: "2e pijler: aanvullend pensioen via werk",
                                               3: "3e pijler: pensioensparen & langetermijnsparen",
                                               4: "4e pijler: eigen sparen & beleggen"}[pil],
                      "value_now": value, "projected_nominal": nom, "projected_today_eur": real,
                      "after_tax_today_eur": r2(after_tax), "monthly_today_eur": r2(per_month), "method": method,
                      "accounts": rows})
    legal = p["legal_pension_monthly"]
    out_p.insert(0, {"pillar": 1, "label": "1e pijler: wettelijk pensioen", "monthly_today_eur": r2(legal),
                     "partner_monthly_today_eur": r2(p["partner_legal_pension_monthly"]),
                     "method": "jouw raming (bruto, euro van vandaag)" if legal is not None else
                     "nog niet ingevuld: zoek de raming op mypension.be en geef ze in (set_pension_profile)",
                     "accounts": []})
    total_m = monthly_income + (legal or 0)

    checks = []
    p3 = pillars[3]
    paid3 = sum(x["premium_this_year"] or 0 for x in p3)
    if not p3:
        checks.append({"kind": "pension_saving_missing", "severity": "info",
                       "text": f"Geen pensioensparen gevonden. In {today.year} kan je tot "
                               f"{_e(PENSION_SAVING_MAX['30%'])} storten (30 % belastingvermindering, ongeveer "
                               f"{_e(PENSION_SAVING_MAX['30%'] * 0.30)}) of {_e(PENSION_SAVING_MAX['25%'])} "
                               f"(25 %), per persoon."})
    elif paid3 < PENSION_SAVING_MAX["30%"] and today.month >= 10:
        checks.append({"kind": "pension_saving_room", "severity": "warning",
                       "text": f"Pensioensparen {today.year}: {_e(paid3)} gestort; de belastingvermindering "
                               f"telt tot {_e(PENSION_SAVING_MAX['30%'])} (of {_e(PENSION_SAVING_MAX['25%'])}). "
                               "Stort vóór 31/12."})
    for x in pillars[2]:
        if x["stale"]:
            checks.append({"kind": "stale_reserve", "severity": "info",
                           "text": f"{x['name']}: de reserve is van {x['value_date']}; geef de waarde van de "
                                   f"laatste pensioenfiche in (meestal in het voorjaar)."})
        if "business" in x["paid_by"]:
            checks.append({"kind": "ipt_80", "severity": "info",
                           "text": f"{x['name']} wordt betaald door je vennootschap: de premie is enkel aftrekbaar "
                                   "binnen de 80 %-regel (wettelijk + aanvullend pensioen samen max. 80 % van je "
                                   "laatste bruto jaarbezoldiging). De verzekeraar berekent die ruimte elk jaar."})
    p4_real = next((x["projected_today_eur"] for x in out_p if x["pillar"] == 4), 0) or 0
    if early and p4_real < 0:
        checks.append({"kind": "runs_out", "severity": "warning",
                       "text": f"Als je op {stop_age} stopt, is je eigen vermogen op vóór je pensioen op "
                               f"{p['retirement_age']}: zie de Planner voor wanneer."})
    if needs:
        checks.append({"kind": "profile", "severity": "info",
                       "text": "Nodig voor de projectie: " + ", ".join(needs) + " (set_pension_profile)."})

    return {
        "today": today.isoformat(), "profile": {k: v for k, v in p.items() if k != "accounts"},
        "account_settings": p["accounts"], "retirement_date": ret_date.isoformat() if ret_date else None,
        "years_to_retirement": round(years, 1) if years is not None else None,
        "stop_work_age": stop_age, "years_paying": round(paying, 1) if paying is not None else None,
        "age_now": round((today - date.fromisoformat(p["birth_date"])).days / 365.25, 1) if p["birth_date"] else None,
        "pillars": out_p, "monthly_income_today_eur": r2(total_m) if years is not None else None,
        "spending_monthly_now": r2(spend_m), "spending_profile": sp, "investing": inv,
        "replacement_ratio": round(total_m / spend_m, 3) if spend_m and years is not None else None,
        "gap_monthly_today_eur": r2(spend_m - total_m) if spend_m and years is not None else None,
        "checks": checks, "needs": needs,
        "limits_2026": {"pension_saving": PENSION_SAVING_MAX, "long_term_saving_max": LONG_TERM_SAVING_MAX},
        "notes": "Projecties in euro van vandaag met de aannames uit het profiel. Vanaf 2027 verlaagt vroeger met "
                 "pensioen gaan het wettelijk pensioen (malus), tenzij je aan de loopbaanvoorwaarden voldoet. De "
                 "officiële cijfers staan op je pensioenfiche (verzekeraar) en op mypension.be.",
    }


# --- pension statements (r18): read the yearly pension fiche of an insurer -----------------------------------

_AMOUNT = re.compile(r"(?:€|eur\b)?\s*(\d{1,3}(?:[.\s ]\d{3})+(?:,\d{1,2})?|\d+,\d{2})\s*(?:€|eur\b)?", re.I)
_DATE = re.compile(r"\b(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})\b")
_KINDS = {
    "reserve": (re.compile(r"verworven\s+reserve|r[eé]serves?\s+acquises?|opgebouwde\s+reserve|reserve\s+op|"
                           r"theoretische\s+afkoopwaarde|afkoopwaarde|valeur\s+de\s+rachat", re.I), 2),
    "expected": (re.compile(r"te\s+verwachten|verwachte\s+(?:prestatie|kapitaal|pensioen)|geraamde?\s+(?:prestatie|"
                            r"kapitaal)|raming|prestations?\s+attendues?|estimation|kapitaal\s+op\s+(?:de\s+)?"
                            r"pensioen|op\s+(?:de\s+)?(?:pensioen|eind)(?:datum|leeftijd)", re.I), 2),
    "premium": (re.compile(r"(?:jaar|jaarlijkse|totale|betaalde|gestorte)\s*premie|premie\s+(?:per\s+jaar|van\s+het\s+"
                           r"jaar|\d{4})|primes?\s+(?:annuelles?|vers[ée]es?)", re.I), 1),
}


def _amount(s: str) -> float | None:
    m = _AMOUNT.findall(s)
    if not m:
        return None
    raw = m[-1].replace(" ", "").replace(" ", "").replace(" ", "").replace(".", "").replace(",", ".")
    try:
        v = float(raw)
    except ValueError:
        return None
    return v if v >= 10 else None


def parse_statement_text(text: str) -> dict:
    """Pick the reserve, the expected capital at the pension date and the yearly premium out of a pension fiche."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    found = {}
    for i, ln in enumerate(lines):
        for kind, (rx, look) in _KINDS.items():
            if kind in found or not rx.search(ln):
                continue
            for j in range(i, min(i + look, len(lines))):  # the amount can be on the next line
                v = _amount(lines[j])
                if v is not None:
                    d = _DATE.search(ln) or _DATE.search(lines[j])
                    found[kind] = {"amount": round(v, 2), "line": ln[:160] + ("" if j == i else " / " + lines[j][:80]),
                                   "date": f"{d.group(3)}-{int(d.group(2)):02d}-{int(d.group(1)):02d}" if d else None}
                    break
    age = re.search(r"(?:pensioen|eind)?leeftijd\s+(?:van\s+)?(\d{2})\s*jaar|op\s+(\d{2})\s*jaar|à\s+(\d{2})\s*ans",
                    text, re.I)
    if age:
        found["age"] = int(next(g for g in age.groups() if g))
    d = re.search(r"(?:situatie|toestand|stand|op datum van|situation)\s+(?:op|au|van)?\s*(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})",
                  text, re.I)
    if d:
        found["statement_date"] = f"{d.group(3)}-{int(d.group(2)):02d}-{int(d.group(1)):02d}"
    return found


def read_statement(path: str) -> str:
    try:
        import pdfplumber
    except ImportError as e:  # pragma: no cover
        raise ValueError("pdfplumber is needed to read PDFs (pip install pdfplumber)") from e
    with pdfplumber.open(path) as pdf:
        return "\n".join(p.extract_text() or "" for p in pdf.pages[:12])


def import_statement(conn, path: str | None = None, account: str | None = None, apply: bool = False,
                     text: str | None = None, today: date | None = None) -> dict:
    """Read a pension fiche (PDF) and, with apply=True, save the reserve as the account's value, the expected capital
    as expected_at_retirement and the yearly premium."""
    today = today or date.today()
    raw = text if text is not None else read_statement(path)
    found = parse_statement_text(raw)
    accs = [a for a in AS.list_accounts(conn, today) if a["type"] in ("pension", "insurance")]
    if account:
        acc = AS.find(conn, account)
        name = acc["name"]
    elif len(accs) == 1:
        name = accs[0]["name"]
    else:
        name = None
    out = {"account": name, "found": found, "applied": False,
           "accounts": [a["name"] for a in accs]}
    if not found:
        out["note"] = ("Niets herkend. Geef de bedragen zelf in (reserve, verwacht kapitaal, premie) of stuur de "
                       "tekst door.")
        return out
    if apply:
        if not name:
            raise ValueError("Kies de pensioenrekening waartoe de fiche hoort.")
        apply_values(conn, name, reserve=(found.get("reserve") or {}).get("amount"),
                     reserve_date=(found.get("reserve") or {}).get("date") or found.get("statement_date"),
                     expected=(found.get("expected") or {}).get("amount"),
                     premium=(found.get("premium") or {}).get("amount"), today=today)
        out["applied"] = True
    return out


def apply_values(conn, account: str, reserve: float | None = None, reserve_date: str | None = None,
                 expected: float | None = None, premium: float | None = None, today: date | None = None) -> dict:
    acc = AS.find(conn, account)
    if reserve is not None:
        AS.set_value(conn, acc["name"], float(reserve), reserve_date, today=today)
    settings = {}
    if expected is not None:
        settings["expected_at_retirement"] = float(expected)
    if premium is not None:
        settings["premium_yearly"] = float(premium)
    if settings:
        set_profile(conn, accounts={acc["name"]: settings})
    return {"account": acc["name"], "reserve": reserve, "reserve_date": reserve_date, "expected": expected,
            "premium": premium}
