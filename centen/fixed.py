"""Subscriptions and fixed costs (r17): everything that comes back on a schedule, grouped the way a household thinks
about it (housing, insurance, subscriptions, children, car, ...), with the monthly and yearly cost, the next date,
price increases, payments that seem to have stopped, overlapping services and the big yearly bills coming up.

Built on `analytics.recurring` (detection from the bank data). On top of that the user can
- ignore a detected item that isn't a fixed cost (a yearly gift, a one-off that happened to repeat),
- confirm or rename one, move it to another group, or mark it cancelled (then it no longer counts, and the forecast
  stops expecting it),
- add fixed costs the bank data can't show (paid by a card of another bank, by the partner, in cash).
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta

from . import analytics as A
from .money import normalize_text

SCHEMA = """
CREATE TABLE IF NOT EXISTS fixed_overrides (
    merchant_key  TEXT NOT NULL,
    scope         TEXT NOT NULL,             -- personal | business
    status        TEXT,                      -- confirmed | ignored | cancelled
    name          TEXT,                      -- display name chosen by the user
    grp           TEXT,                      -- group chosen by the user
    note          TEXT,
    updated_at    TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (merchant_key, scope)
);
CREATE TABLE IF NOT EXISTS fixed_manual (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    amount        REAL NOT NULL,             -- per occurrence, positive = cost
    cadence       TEXT NOT NULL,             -- weekly | monthly | quarterly | every 6 months | yearly | ...
    next_date     TEXT,                      -- next expected payment
    grp           TEXT,
    scope         TEXT DEFAULT 'personal',
    paid_by       TEXT,                      -- free text: "Visa ING", "Dominique", "cash"
    note          TEXT,
    active        INTEGER DEFAULT 1,
    created_at    TEXT DEFAULT (datetime('now'))
);
"""

GROUPS = ["Wonen & energie", "Lening", "Verzekeringen", "Abonnementen", "Kinderen", "Mobiliteit", "Gezondheid",
          "Lidmaatschappen", "Belastingen", "Personeel & advies", "Naar jou (bezoldiging, huur)", "Bankkosten",
          "Sparen & beleggen", "Overige"]

_BY_CATEGORY = {
    "Rent": "Wonen & energie", "Energy": "Wonen & energie", "Water": "Wonen & energie",
    "Telecom & internet": "Wonen & energie", "Home & DIY": "Wonen & energie",
    "Loans & mortgage": "Lening",
    "Insurance": "Verzekeringen", "Health insurance (mutualiteit)": "Verzekeringen",
    "Subscriptions & streaming": "Abonnementen", "News & magazines": "Abonnementen", "Leisure & sport": "Abonnementen",
    "Childcare & school": "Kinderen", "Education": "Kinderen",
    "Car & maintenance": "Mobiliteit", "Fuel": "Mobiliteit", "EV charging": "Mobiliteit",
    "Public transport & taxi": "Mobiliteit", "Parking & tolls": "Mobiliteit",
    "Healthcare": "Gezondheid",
    "Taxes & social security": "Belastingen", "VAT": "Belastingen", "Corporate tax": "Belastingen",
    "Withholding tax": "Belastingen",
    "Payroll & social secretariat": "Personeel & advies", "Accounting & advisory": "Personeel & advies",
    "Bank fees": "Bankkosten",
    "Own accounts": "Sparen & beleggen", "Savings & investments": "Sparen & beleggen",
    "Gifts & donations": "Lidmaatschappen",
}
# Words in the merchant name that say what a payment is better than its category does.
_BY_NAME = [
    ("Wonen & energie", ("energy", "engie", "luminus", "eneco", "totalenergies", "mega ", "bolt energie", "fluvius",
                         "water-link", "de watergroep", "farys", "pidpa", "proximus", "telenet", "orange", "base ",
                         "scarlet", "mobile vikings", "digi ", "poetsbureau", "dienstencheque", "pluxee", "sodexo")),
    ("Verzekeringen", ("verzekering", "insurance", "assurance", "ethias", "baloise", "ag insurance", "axa", "allianz",
                       "dvv", "p&v", "vivium", "mutualiteit", "ziekenfonds", "cm ", "helan", "partena", "solidaris")),
    ("Abonnementen", ("spotify", "netflix", "disney", "apple.com", "apple com", "icloud", "google", "youtube",
                      "amazon prime", "prime video", "hbo", "max ", "streamz", "viaplay", "deezer", "dropbox",
                      "microsoft", "adobe", "chatgpt", "openai", "anthropic", "claude", "github", "playstation",
                      "xbox", "nintendo", "audible", "standaard", "nieuwsblad", "tijd", "knack", "humo", "gva",
                      "basic-fit", "basic fit", "jims", "fitness", "padel")),
    ("Lidmaatschappen", ("acv", "abvv", "aclvb", "vakbond", "unizo", "vab", "touring", "natuurpunt", "unicef",
                         "artsen zonder grenzen", "11.11.11", "rode kruis")),
    ("Kinderen", ("kinderopvang", "creche", "kribbe", "school", "academie", "scouts", "chiro", "speelplein")),
    ("Mobiliteit", ("lease", "leasing", "athlon", "arval", "ayvens", "kmo-auto", "nmbs", "de lijn", "cambio")),
]
# Groups where two or more active services may overlap (several streaming services, two gyms, ...).
_OVERLAP_CATEGORIES = {"Subscriptions & streaming", "News & magazines", "Leisure & sport", "Telecom & internet"}
NOMINAL = {c[0]: c[1] for c in A.CADENCES}
_CADENCE_ALIASES = {"week": "weekly", "wekelijks": "weekly", "maand": "monthly", "maandelijks": "monthly",
                    "kwartaal": "quarterly", "per kwartaal": "quarterly", "halfjaarlijks": "every 6 months",
                    "jaar": "yearly", "jaarlijks": "yearly", "tweemaandelijks": "every 2 months",
                    "tweewekelijks": "every 2 weeks"}


def ensure(conn) -> None:
    conn.executescript(SCHEMA)


def r2(x):
    return None if x is None else round(float(x), 2)


def cadence_name(c: str) -> str:
    c = (c or "").strip().lower()
    c = _CADENCE_ALIASES.get(c, c)
    if c not in NOMINAL:
        raise ValueError(f"Unknown cadence '{c}'. Use one of: {', '.join(NOMINAL)}.")
    return c


def group_for(category: str | None, merchant: str | None, internal: bool = False) -> str:
    if internal:
        return "Naar jou (bezoldiging, huur)"
    n = f" {normalize_text(merchant or '')} "
    if category not in ("Own accounts", "Savings & investments", "Loans & mortgage", "Taxes & social security",
                        "VAT", "Corporate tax", "Withholding tax", "Payroll & social secretariat"):
        for grp, words in _BY_NAME:
            if any(f" {w.strip()} " in n or (len(w.strip()) > 5 and w.strip() in n) for w in words):
                return grp
    return _BY_CATEGORY.get(category or "", "Overige")


def _overrides(conn) -> dict:
    ensure(conn)
    return {(r["merchant_key"], r["scope"]): dict(r) for r in conn.execute("SELECT * FROM fixed_overrides")}


def _internal_keys(conn) -> set[str]:
    """Merchant keys of payments between the company and the user (salary, rent to the owner)."""
    return {r[0] for r in conn.execute("SELECT DISTINCT merchant_key FROM transactions WHERE internal='cross' "
                                       "AND merchant_key IS NOT NULL")}


def _nominal(cadence: str) -> float:
    return NOMINAL.get(cadence, 30.44)


def _manual_next(row, today: date) -> date | None:
    if not row["next_date"]:
        return None
    d = date.fromisoformat(row["next_date"][:10])
    n = 0
    while d < today and n < 500:
        d = A.add_months(d, 1) if row["cadence"] == "monthly" else d + timedelta(days=round(_nominal(row["cadence"])))
        n += 1
    return d


def items(conn, today: date | None = None, perspective: str | None = None, include_ignored: bool = False,
          lookback_days: int = 460) -> list[dict]:
    """Fixed costs (money out) as a flat list: detected ones with the user's overrides applied, plus manual ones."""
    today = today or date.today()
    ov = _overrides(conn)
    internal = _internal_keys(conn)
    out = []
    rec = A.recurring(conn, today, lookback_days=lookback_days, include_income=False, include_inactive=True,
                      perspective=perspective if perspective in ("personal", "business") else None)
    for r in rec:
        if r["direction"] != "out":
            continue
        o = ov.get((r["merchant_key"], r["scope"]), {})
        status = o.get("status") or ("active" if r["active"] else "stopped")
        if status == "ignored" and not include_ignored:
            continue
        if status == "confirmed":
            status = "active" if r["active"] else "stopped"
        is_internal = r["merchant_key"] in internal and r["category"] in ("Director remuneration", "Rent")
        grp = o.get("grp") or group_for(r["category"], r["merchant"], is_internal)
        nominal = _nominal(r["cadence"])
        counts = status == "active"
        out.append({
            "key": r["merchant_key"], "name": o.get("name") or r["merchant"], "merchant": r["merchant"],
            "group": grp, "category": r["category"], "scope": r["scope"], "cadence": r["cadence"],
            "amount": r["typical_amount"], "last_amount": r["last_amount"], "last_date": r["last_date"],
            "next_date": r["next_expected"] if counts else None, "occurrences": r["occurrences"],
            "paid_by": r["paid_by"], "monthly": r2(r["typical_amount"] * 30.44 / nominal) if counts else 0.0,
            "yearly": r2(r["typical_amount"] * 365.25 / nominal) if counts else 0.0,
            "price_change": r["price_change"], "status": status, "source": "detected", "internal": is_internal,
            "confirmed": o.get("status") == "confirmed", "note": o.get("note"), "account_id": r["account_id"],
        })
    ensure(conn)
    for m in conn.execute("SELECT * FROM fixed_manual ORDER BY id"):
        scope = m["scope"] or "personal"
        if perspective in ("personal", "business") and scope != perspective:
            continue
        if not m["active"] and not include_ignored:
            continue
        cad = m["cadence"]
        nominal = _nominal(cad)
        nxt = _manual_next(m, today)
        active = bool(m["active"])
        out.append({
            "key": f"manual:{m['id']}", "id": m["id"], "name": m["name"], "merchant": m["name"],
            "group": m["grp"] or group_for(None, m["name"]), "category": None, "scope": scope, "cadence": cad,
            "amount": r2(m["amount"]), "last_amount": None, "last_date": None,
            "next_date": nxt.isoformat() if nxt and active else None, "occurrences": None,
            "paid_by": m["paid_by"] or "entered", "monthly": r2(m["amount"] * 30.44 / nominal) if active else 0.0,
            "yearly": r2(m["amount"] * 365.25 / nominal) if active else 0.0, "price_change": None,
            "status": "active" if active else "cancelled", "source": "manual", "internal": False, "confirmed": True,
            "note": m["note"], "account_id": None,
        })
    out.sort(key=lambda x: (GROUPS.index(x["group"]) if x["group"] in GROUPS else 99, -(x["monthly"] or 0)))
    return out


def report(conn, today: date | None = None, perspective: str | None = None) -> dict:
    today = today or date.today()
    its = items(conn, today, perspective)
    active = [i for i in its if i["status"] == "active"]
    totals = {}
    for scope in ("personal", "business"):
        sc = [i for i in active if i["scope"] == scope]
        if not sc:
            continue
        costs = [i for i in sc if i["group"] != "Sparen & beleggen"]
        ext = [i for i in costs if not i["internal"]]
        totals[scope] = {"monthly": r2(sum(i["monthly"] for i in costs)), "yearly": r2(sum(i["yearly"] for i in costs)),
                         "monthly_excl_internal": r2(sum(i["monthly"] for i in ext)),
                         "yearly_excl_internal": r2(sum(i["yearly"] for i in ext)),
                         "saving_monthly": r2(sum(i["monthly"] for i in sc if i["group"] == "Sparen & beleggen")),
                         "count": len(costs)}
    groups = defaultdict(lambda: {"monthly": 0.0, "yearly": 0.0, "count": 0})
    for i in active:
        g = groups[(i["scope"], i["group"])]
        g["monthly"] += i["monthly"]
        g["yearly"] += i["yearly"]
        g["count"] += 1
    group_list = [{"scope": s, "group": g, "monthly": r2(v["monthly"]), "yearly": r2(v["yearly"]), "count": v["count"]}
                  for (s, g), v in groups.items()]
    group_list.sort(key=lambda x: (x["scope"] != "personal", -x["monthly"]))

    increases = []
    for i in its:
        pc = i["price_change"]
        if not pc or i["status"] != "active":
            continue
        since = date.fromisoformat(pc["since"])
        if (today - since).days > 365:
            continue
        per_year = (pc["to"] - pc["from"]) * 365.25 / _nominal(i["cadence"])
        increases.append({"name": i["name"], "scope": i["scope"], "group": i["group"], "from": pc["from"], "to": pc["to"],
                          "pct": pc["pct"], "since": pc["since"], "per_year": r2(per_year), "cadence": i["cadence"],
                          "direction": "up" if pc["pct"] > 0 else "down"})
    increases.sort(key=lambda x: -abs(x["per_year"] or 0))

    stopped = [{"name": i["name"], "scope": i["scope"], "group": i["group"], "amount": i["amount"],
                "cadence": i["cadence"], "last_date": i["last_date"], "yearly_before": r2(i["amount"] * 365.25 / _nominal(i["cadence"]))}
               for i in its if i["status"] == "stopped" and i["last_date"]
               and (today - date.fromisoformat(i["last_date"])).days <= 240]

    overlaps = []
    by_cat = defaultdict(list)
    for i in active:
        if i["category"] in _OVERLAP_CATEGORIES and not i["internal"]:
            by_cat[(i["scope"], i["category"])].append(i)
    for (scope, catg), lst in by_cat.items():
        if len(lst) >= 2:
            overlaps.append({"scope": scope, "category": catg, "names": [x["name"] for x in lst],
                             "yearly": r2(sum(x["yearly"] for x in lst))})

    horizon = today + timedelta(days=120)
    big_coming = [{"name": i["name"], "scope": i["scope"], "date": i["next_date"], "amount": i["amount"],
                   "cadence": i["cadence"]}
                  for i in active if i["next_date"] and i["cadence"] in ("quarterly", "every 6 months", "yearly")
                  and (i["amount"] or 0) >= 100 and date.fromisoformat(i["next_date"]) <= horizon]
    big_coming.sort(key=lambda x: x["date"])

    rec_in = [r for r in A.recurring(conn, today, include_income=True,
                                     perspective=perspective if perspective in ("personal", "business") else None)
              if r["direction"] == "in"]
    income = [{"name": r["merchant"], "category": r["category"], "scope": r["scope"], "cadence": r["cadence"],
               "amount": r["typical_amount"], "monthly": r["monthly_equivalent"], "next_date": r["next_expected"]}
              for r in rec_in]

    # Share of income that's committed: personal fixed costs (excluding saving) vs regular personal income.
    commit = None
    if "personal" in totals:
        inc = sum(x["monthly"] for x in income if x["scope"] == "personal")
        if inc > 0:
            commit = round(totals["personal"]["monthly"] / inc, 3)
    public = [{k: v for k, v in i.items() if k != "account_id"} for i in its]
    return {"today": today.isoformat(), "totals": totals, "groups": group_list, "items": public,
            "price_changes": increases, "stopped": stopped, "overlaps": overlaps, "big_coming": big_coming,
            "income": income, "fixed_share_of_income": commit,
            "notes": "Detected from the bank data (direct debits, standing orders, regular card and transfer payments) "
                     "plus fixed costs entered by hand. Yearly = typical amount × occurrences per year. Card "
                     "subscriptions on a card of a bank that isn't linked can't be seen: add them with add_fixed_cost."}


# --- user actions -------------------------------------------------------------------------------

def _find_detected(conn, name: str, today: date) -> list[dict]:
    n = normalize_text(name)
    its = items(conn, today, include_ignored=True)
    hits = [i for i in its if i["source"] == "detected" and (n == normalize_text(i["name"]) or n == i["key"])]
    if not hits:
        hits = [i for i in its if i["source"] == "detected" and (n in normalize_text(i["name"]) or n in i["key"])]
    return hits


def set_item(conn, name: str, status: str | None = None, new_name: str | None = None, group: str | None = None,
             note: str | None = None, scope: str | None = None, today: date | None = None) -> dict:
    """Confirm, ignore, mark cancelled, rename or regroup a detected fixed cost; or stop / restart a manual one."""
    today = today or date.today()
    ensure(conn)
    if status is not None and status not in ("confirmed", "ignored", "cancelled", "active"):
        raise ValueError("status must be confirmed, ignored, cancelled or active")
    if group is not None and group not in GROUPS:
        raise ValueError(f"group must be one of: {', '.join(GROUPS)}")
    manual = conn.execute("SELECT * FROM fixed_manual WHERE lower(name)=lower(?)", (name,)).fetchone()
    if manual is None and name.startswith("manual:"):
        manual = conn.execute("SELECT * FROM fixed_manual WHERE id=?", (int(name.split(":")[1]),)).fetchone()
    if manual is not None:
        if status in ("ignored", "cancelled"):
            conn.execute("UPDATE fixed_manual SET active=0 WHERE id=?", (manual["id"],))
        elif status in ("active", "confirmed"):
            conn.execute("UPDATE fixed_manual SET active=1 WHERE id=?", (manual["id"],))
        if new_name or group or note is not None:
            conn.execute("UPDATE fixed_manual SET name=COALESCE(?,name), grp=COALESCE(?,grp), note=COALESCE(?,note) "
                         "WHERE id=?", (new_name, group, note, manual["id"]))
        conn.commit()
        return {"updated": manual["name"], "source": "manual"}
    hits = _find_detected(conn, name, today)
    if scope:
        hits = [h for h in hits if h["scope"] == scope]
    keys = {(h["key"], h["scope"]) for h in hits}
    if not keys:
        raise ValueError(f"No fixed cost matches '{name}'. Use fixed_costs to see the names.")
    if len(keys) > 1:
        raise ValueError(f"'{name}' matches several: {', '.join(sorted({h['name'] + ' (' + h['scope'] + ')' for h in hits}))}. "
                         "Be more specific or pass scope.")
    key, sc = keys.pop()
    cur = _overrides(conn).get((key, sc), {})
    st = None if status == "active" else (status if status is not None else cur.get("status"))
    conn.execute("INSERT INTO fixed_overrides(merchant_key, scope, status, name, grp, note) VALUES (?,?,?,?,?,?) "
                 "ON CONFLICT(merchant_key, scope) DO UPDATE SET status=excluded.status, name=excluded.name, "
                 "grp=excluded.grp, note=excluded.note, updated_at=datetime('now')",
                 (key, sc, st, new_name or cur.get("name"), group or cur.get("grp"),
                  note if note is not None else cur.get("note")))
    conn.commit()
    return {"updated": hits[0]["name"], "scope": sc, "status": st or "detected", "source": "detected"}


def add_manual(conn, name: str, amount: float, cadence: str, next_date: str | None = None, group: str | None = None,
               scope: str = "personal", paid_by: str | None = None, note: str | None = None) -> dict:
    ensure(conn)
    cad = cadence_name(cadence)
    if amount <= 0:
        raise ValueError("amount is the cost per payment, a positive number")
    if group is not None and group not in GROUPS:
        raise ValueError(f"group must be one of: {', '.join(GROUPS)}")
    if scope not in ("personal", "business"):
        raise ValueError("scope must be personal or business")
    if next_date:
        date.fromisoformat(next_date[:10])
    cur = conn.execute("INSERT INTO fixed_manual(name, amount, cadence, next_date, grp, scope, paid_by, note) "
                       "VALUES (?,?,?,?,?,?,?,?)", (name.strip(), float(amount), cad, next_date, group, scope, paid_by,
                                                    note))
    conn.commit()
    return {"added": name.strip(), "id": cur.lastrowid, "cadence": cad,
            "yearly": r2(amount * 365.25 / _nominal(cad))}


def remove_manual(conn, name: str) -> dict:
    ensure(conn)
    row = conn.execute("SELECT id, name FROM fixed_manual WHERE lower(name)=lower(?)", (name,)).fetchone()
    if row is None:
        raise ValueError(f"No fixed cost entered by hand is called '{name}'.")
    conn.execute("DELETE FROM fixed_manual WHERE id=?", (row["id"],))
    conn.commit()
    return {"removed": row["name"]}


def expected_payments(conn, start: date, end: date, today: date | None = None) -> list[dict]:
    """Manual fixed costs as dated payments between start and end (for the forecast)."""
    today = today or date.today()
    ensure(conn)
    out = []
    for m in conn.execute("SELECT * FROM fixed_manual WHERE active=1"):
        d = _manual_next(m, start)
        n = 0
        while d and d <= end and n < 400:
            out.append({"date": d.isoformat(), "name": m["name"], "amount": -float(m["amount"]),
                        "scope": m["scope"] or "personal", "group": m["grp"] or group_for(None, m["name"])})
            d = A.add_months(d, 1) if m["cadence"] == "monthly" else d + timedelta(days=round(_nominal(m["cadence"])))
            n += 1
    return out


def excluded_keys(conn) -> set[tuple[str, str]]:
    """(merchant_key, scope) the user ignored or cancelled: the forecast doesn't expect them any more."""
    return {k for k, v in _overrides(conn).items() if v.get("status") in ("ignored", "cancelled")}
