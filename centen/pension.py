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
    "legal_pension_monthly": None,    # estimate from mypension.be, gross per month, today's euros
    "partner_legal_pension_monthly": None,
    "return_pension": 0.025,          # pillars 2 and 3, nominal per year
    "return_investments": 0.05,       # pillar 4, nominal per year
    "inflation": 0.02,
    "payout_tax": 0.165,              # rough total tax and contributions on a 2nd pillar capital at pension age
    "withdrawal_rate": 0.035,         # pillar 4: what you can take out per year
    "live_until": 90,
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


def set_profile(conn, **changes) -> dict:
    p = profile(conn)
    accounts = changes.pop("accounts", None) or {}
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
        elif k in ("retirement_age", "live_until"):
            v = int(v)
            if not 50 <= v <= 110:
                raise ValueError(f"{k} looks wrong: {v}")
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


def _fv(value: float, premium: float, r: float, years: float) -> float:
    if years <= 0:
        return value
    g = (1 + r) ** years
    return value * g + (premium * (g - 1) / r if r else premium * years)


def _premiums(conn, acc_name: str, today: date) -> tuple[float, float]:
    """Money into the account in the last 12 months and in this calendar year."""
    acc = AS.find(conn, acc_name)
    flows, _ = AS.contribution_flows(conn, acc["id"])
    y0 = today - timedelta(days=365)
    last12 = sum(f[1] for f in flows if date.fromisoformat(f[0][:10]) > y0)
    ytd = sum(f[1] for f in flows if f[0][:4] == str(today.year))
    return max(0.0, last12), max(0.0, ytd)


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

    pillars = {2: [], 3: [], 4: []}
    for a in AS.list_accounts(conn, today):
        if a["type"] not in ("pension", "insurance", "investment", "crypto"):
            continue
        if a["scope"] != "personal" and a["type"] != "pension":
            continue  # company investments are the company's, not your pension
        s = p["accounts"].get(a["name"]) or {}
        pil = pillar_of(a, s)
        value = AS.counted_value(a)
        last12, ytd = _premiums(conn, a["name"], today)
        premium = s.get("premium_yearly", last12 if pil != 4 else 0.0)
        r = p["return_pension"] if pil in (2, 3) else p["return_investments"]
        proj = _fv(value, premium, r, years) if years is not None else None
        exp = s.get("expected_at_retirement")
        row = {"name": a["name"], "provider": a.get("provider"), "type": a["type"], "pillar": pil, "value": r2(value),
               "value_date": (a.get("value_entered") or {}).get("date"), "contributions": a.get("contributions"),
               "premium_last_12m": r2(last12), "premium_this_year": r2(ytd), "premium_assumed": r2(premium),
               "paid_by": sorted((a.get("contributions_by_payer") or {a["scope"]: 1}).keys()),
               "return_assumed": r, "projected_nominal": r2(proj),
               "projected_today_eur": r2(proj / infl) if proj is not None else None,
               "expected_at_retirement": r2(exp), "expected_today_eur": r2(exp / infl) if exp and years is not None else None,
               "stale": a.get("stale", False)}
        pillars[pil].append(row)

    def cap(rows, key_nom, key_real):
        nom = sum((x["expected_at_retirement"] or x[key_nom] or 0) for x in rows)
        real = sum((x["expected_today_eur"] or x[key_real] or 0) for x in rows)
        return r2(nom), r2(real)

    spend = A.breakdown(conn, today - timedelta(days=365), today, perspective="personal")["total"]
    spend_m = spend / 12 if spend else None
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
            per_month = (real or 0) * p["withdrawal_rate"] / 12 if years is not None else None
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
                     "method": "raming van mypension.be (bruto, euro van vandaag)" if legal is not None else
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
    if needs:
        checks.append({"kind": "profile", "severity": "info",
                       "text": "Nodig voor de projectie: " + ", ".join(needs) + " (set_pension_profile)."})

    return {
        "today": today.isoformat(), "profile": {k: v for k, v in p.items() if k != "accounts"},
        "account_settings": p["accounts"], "retirement_date": ret_date.isoformat() if ret_date else None,
        "years_to_retirement": round(years, 1) if years is not None else None,
        "age_now": round((today - date.fromisoformat(p["birth_date"])).days / 365.25, 1) if p["birth_date"] else None,
        "pillars": out_p, "monthly_income_today_eur": r2(total_m) if years is not None else None,
        "spending_monthly_now": r2(spend_m),
        "replacement_ratio": round(total_m / spend_m, 3) if spend_m and years is not None else None,
        "gap_monthly_today_eur": r2(spend_m - total_m) if spend_m and years is not None else None,
        "checks": checks, "needs": needs,
        "limits_2026": {"pension_saving": PENSION_SAVING_MAX, "long_term_saving_max": LONG_TERM_SAVING_MAX},
        "notes": "Projecties in euro van vandaag met de aannames uit het profiel. Vanaf 2027 verlaagt vroeger met "
                 "pensioen gaan het wettelijk pensioen (malus), tenzij je aan de loopbaanvoorwaarden voldoet. De "
                 "officiële cijfers staan op je pensioenfiche (verzekeraar) en op mypension.be.",
    }
