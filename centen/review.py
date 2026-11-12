"""Clearing out "Other": the payments no rule recognised, grouped per merchant with a suggested category, and a
way to label a whole group at once (a rule for the future plus the payments already there).

Labelling only changes the local copy (a row in category_rules, or hand-picked categories), never anything at the
bank, and can be undone with `centen rules delete`.
"""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import date

from . import categorize as C
from .money import mask_ibans_in_text, normalize_text

UNLABELLED = ("Other expenses", "Other income")

# Words in the counterparty / description that point to a category. Checked in order; the first hit wins.
HINTS: list[tuple[str, str]] = [
    ("Home & DIY", r"tuin|garden|hovenier|schilder|loodgiet|sanitair|dakwerk|elektro(?!nica)|elektricien|"
                   r"window ?care|glazenwas|ramen ?was|poets|schoonmaak|cleaning|interieur|meubel|keuken|"
                   r"verwarming|ventilatie|aannemer|renovat|bouw|vloer|parket|gordijn|zonwering|tegels"),
    ("Car & maintenance", r"dieteren|garage|carrosserie|bandencentrum|banden|autobanden|\bkia\b|\bbmw\b|porsche|"
                          r"audi|volkswagen|\bvw\b|mercedes|toyota|tesla|autowas|carwash|car wash|keuring|autoveiling"),
    ("Taxes & social security", r"fod financ|spf financ|belastingdienst|vlabel|\bfiscal|gemeentebelast|"
                                r"provinciebelast|compte citoyen|stad .* belast|parkeerretributie"),
    ("Travel & holidays", r"travel|reis|vakantie|hotel|booking|airbnb|camping|ryanair|brussels airlines|tui\b|"
                          r"embarcacion|boat|boot|ferry|resort|apartament|holiday|sunweb|vliegtuig|airport"),
    ("Leisure & sport", r"sport|fitness|gym|padel|tennis|golf|zwembad|ticket|tickets|concert|cinema|kinepolis|"
                        r"museum|pretpark|plopsa|walibi|bowling|escape|monkey ?bike|fiets|velo|bike|ski\b"),
    ("Drugstore & personal care", r"beauty|kapper|kapsalon|coiffure|nagel|nail|schoonheid|esthetiek|spa\b|"
                                  r"massage|barber|parfum|cosmetic|paula'?s ?choice|kruidvat|\bici paris"),
    ("Healthcare", r"apotheek|pharmac|tandarts|dokter|huisarts|kinesi|ziekenhuis|kliniek|opticien|optiek|"
                   r"labo|psycholoog|osteopa|dierenarts"),
    ("Restaurants & takeaway", r"sodexo|restaurant|brasserie|bistro|pizzeria|frituur|sushi|kebab|eethuis|taverne|"
                               r"\bcafe\b|koffie|coffee|bakkerij|traiteur|catering|deliveroo|uber ?eats|takeaway"),
    ("Groceries", r"groothandel|slagerij|beenhouw|supermarkt|markt|bio\b|primeur|kaas|wijnhandel|drankcentrale|"
                  r"carrefour|delhaize|colruyt|aldi|lidl|okay|spar\b"),
    ("Clothing & shoes", r"schoen|shoes|kleding|fashion|boutique|mode\b|textiel|jeans|lingerie"),
    ("Childcare & school", r"school|kinderopvang|creche|kribbe|onthaalmoeder|speelplein|scouts|chiro|academie"),
    ("Gifts & donations", r"bloem|fleur|flower|gift|cadeau|vzw|asbl|donatie|11\.11\.11|unicef|artsen zonder"),
    ("Electronics", r"electro|elektronica|mediamarkt|coolblue|vandenborre|krefel|apple|samsung|computer"),
    ("Insurance", r"verzeker|assurance|insurance|\bpolis\b"),
    ("Accounting & advisory", r"boekhoud|accountan|fiscalist|advocaat|notaris|partners\b|consult"),
]
_HINTS = [(cat, re.compile(rx)) for cat, rx in HINTS]

MCC = {"5411": "Groceries", "5422": "Groceries", "5441": "Groceries", "5451": "Groceries", "5462": "Groceries",
       "5499": "Groceries", "5812": "Restaurants & takeaway", "5813": "Restaurants & takeaway",
       "5814": "Restaurants & takeaway", "5541": "Fuel", "5542": "Fuel", "4111": "Public transport & taxi",
       "4121": "Public transport & taxi", "4131": "Public transport & taxi", "7523": "Parking & tolls",
       "4784": "Parking & tolls", "7011": "Travel & holidays", "4511": "Travel & holidays", "4722": "Travel & holidays",
       "5912": "Healthcare", "8011": "Healthcare", "8021": "Healthcare", "8099": "Healthcare", "5311": "Shopping",
       "5310": "Shopping", "5651": "Clothing & shoes", "5661": "Clothing & shoes", "5691": "Clothing & shoes",
       "5732": "Electronics", "5734": "Electronics", "7230": "Drugstore & personal care", "7298": "Drugstore & personal care",
       "5200": "Home & DIY", "5211": "Home & DIY", "5251": "Home & DIY", "5712": "Home & DIY", "5261": "Home & DIY",
       "7997": "Leisure & sport", "7941": "Leisure & sport", "7832": "Leisure & sport", "5941": "Leisure & sport",
       "4814": "Telecom & internet", "4899": "Subscriptions & streaming", "5992": "Gifts & donations",
       "8220": "Education", "8211": "Childcare & school", "7538": "Car & maintenance", "5533": "Car & maintenance",
       "9311": "Taxes & social security", "6300": "Insurance", "5995": "Pets", "0742": "Pets"}

INCOME_HINTS = [("Benefits & allowances", re.compile(r"premie|veka|mijn verbouwpremie|subsidie|tegemoetkoming")),
                ("Refunds", re.compile(r"terugbetal|refund|rembours|retour|waarborg|deposit|cashback|annul|credit nota"))]

_BUSINESSY = re.compile(r"invest|consult|group|groep|service|global|solution|shop|store|exclusive|trading|"
                        r"company|holding|partners|management|design|studio|center|centrum|international|\bint\b|"
                        r"\bbv\b|\bnv\b|\bvof\b|\bcomm\b|hoofdrekening")
_PERSON = re.compile(r"p2p|payconiq|^[a-z]+ (?:van |de |van de |van der |le |la )?[a-z]+$")


def r2(x):
    return None if x is None else round(float(x), 2)


def _suggest(rows: list[dict], person_names: set[str], refunded_keys: set[str] = frozenset()) -> dict | None:
    income = rows[0]["amount"] > 0
    text = " ".join(normalize_text(" ".join(x for x in (r.get("counterparty_name"), r.get("description"),
                                                         r.get("remittance")) if x)) for r in rows[:6])
    if income:
        for cat, rx in INCOME_HINTS:
            m = rx.search(text)
            if m:
                return {"category": cat, "reason": f"bevat “{m.group(0).strip()}”"}
        if rows[0]["merchant_key"] in refunded_keys:
            return {"category": "Refunds", "reason": "je betaalde eerder aan dezelfde begunstigde"}
    for r in ([] if income else rows):
        if r.get("mcc") and str(r["mcc"]) in MCC:
            return {"category": MCC[str(r["mcc"])], "reason": f"kaartcode (MCC) {r['mcc']}"}
    for cat, rx in ([] if income else _HINTS):
        m = rx.search(text)
        if m:
            return {"category": cat, "reason": f"bevat “{m.group(0).strip()}”"}
    name = normalize_text(rows[0].get("counterparty_name") or rows[0].get("merchant") or "")
    if (_PERSON.search(name) and not _BUSINESSY.search(name)) or any(t and t in name for t in person_names):
        return {"category": None, "reason": "mogelijk een persoon: terugbetaling, cadeau of gedeelde kosten?",
                "person": True}
    return None


def _group_rows(conn, scope: str | None, date_from: str | None):
    q = ("SELECT t.*, a.scope AS acc_scope, a.name AS acc_name FROM transactions t JOIN accounts a ON a.id=t.account_id "
         "WHERE t.status='booked' AND t.category IN (?,?) AND COALESCE(t.category_source,'') != 'user'")
    p: list = list(UNLABELLED)
    if scope:
        q += " AND COALESCE(a.scope,'personal')=?"
        p.append(scope)
    if date_from:
        q += " AND t.booking_date >= ?"
        p.append(date_from)
    groups = defaultdict(list)
    for r in conn.execute(q + " ORDER BY t.booking_date DESC", p):
        groups[(r["merchant_key"] or "unknown", r["category"])].append(dict(r))
    return groups


def queue(conn, scope: str | None = "personal", limit: int = 30, date_from: str | None = None,
          today: date | None = None) -> dict:
    """Groups of unlabelled payments ('Other expenses' / 'Other income'), largest first."""
    today = today or date.today()
    groups = _group_rows(conn, scope, date_from)
    persons = {normalize_text(n) for n in _person_tokens(conn)}
    total_spend = conn.execute(
        "SELECT -SUM(t.amount) FROM transactions t JOIN accounts a ON a.id=t.account_id WHERE t.status='booked' AND "
        "t.kind='expense' AND t.amount<0 AND COALESCE(t.internal,'')='' AND (? IS NULL OR COALESCE(a.scope,'personal')=?)"
        " AND (? IS NULL OR t.booking_date >= ?)", (scope, scope, date_from, date_from)).fetchone()[0] or 0.0
    items = []
    paid_to = {k for (k, cat) in groups if cat == "Other expenses"} | {r[0] for r in conn.execute(
        "SELECT DISTINCT merchant_key FROM transactions WHERE amount < 0 AND kind='expense'")}
    for (key, cat), rows in groups.items():
        amt = sum(r["amount"] for r in rows)
        sug = _suggest(rows, persons, paid_to)
        items.append({
            "merchant": rows[0]["merchant"] or rows[0]["counterparty_name"] or key, "merchant_key": key,
            "now": cat, "count": len(rows), "total": r2(amt), "first": rows[-1]["booking_date"],
            "last": rows[0]["booking_date"], "accounts": sorted({r["acc_name"] for r in rows}),
            "scope": rows[0]["acc_scope"] or "personal",
            "payment_types": sorted({r["payment_type"] for r in rows if r["payment_type"]}),
            "example": mask_ibans_in_text(" · ".join(x for x in (rows[0]["description"], rows[0]["remittance"]) if x)
                                          )[:110] or None,
            "suggestion": sug, "transaction_ids": [r["id"] for r in rows][:50],
        })
    items.sort(key=lambda i: -abs(i["total"]))
    unl_out = -sum(i["total"] for i in items if i["now"] == "Other expenses")
    return {"scope": scope or "all", "groups": len(items), "transactions": sum(i["count"] for i in items),
            "other_expenses": r2(unl_out), "share_of_spending": round(unl_out / total_spend, 4) if total_spend else None,
            "with_suggestion": sum(1 for i in items if i["suggestion"] and i["suggestion"].get("category")),
            "items": items[:max(1, min(limit, 500))],
            "how": "label_merchant(merchant, category) maakt een regel voor de toekomst en past ze toe op wat er al is."}


def _person_tokens(conn) -> list[str]:
    from . import config
    try:
        members = config.load().get("household_members") or {}
    except Exception:  # noqa: BLE001
        members = {}
    out = []
    for name, aliases in members.items():
        out += [name] + list(aliases or [])
    return out


def _text(r) -> str:
    return normalize_text(" | ".join(x for x in (r["counterparty_name"], r["description"], r["remittance"]) if x))


_LEGAL_TAIL = re.compile(r"(?:\s+(?:nv|bv|sa|srl|sprl|bvba|vzw|asbl|cv|cvba|ag|gmbh|ltd|inc|bank))+\s*$")


def _pattern_for(rows: list[dict]) -> str | None:
    """A rule pattern that matches every payment in the group: the counterparty name as banks write it."""
    names = []
    for r in rows:
        n = normalize_text(r.get("counterparty_name") or "")
        n = _LEGAL_TAIL.sub("", re.sub(r"\s{2,}.*$", "", n)).strip()  # "travelbase int.        gent" -> name only
        if len(n) >= 4 and n not in names:
            names.append(n)
    if not names:
        m = normalize_text(rows[0].get("merchant") or "")
        names = [m] if len(m) >= 4 else []
    if not names:
        return None
    pats = [r"\b" + re.escape(n) + r"\b" for n in names[:4]]
    pat = "|".join(pats)
    rx = re.compile(pat)
    if all(rx.search(_text(r)) for r in rows):
        return pat
    return None


def label(conn, merchant: str, category: str, scope: str | None = None, dry_run: bool = False,
          rule: bool = True) -> dict:
    """Label every unlabelled payment of one merchant (name or merchant_key) as `category`. rule=True also adds a rule
    so future payments get it too; when no safe pattern exists (e.g. only an account number), the payments
    themselves are labelled by hand."""
    if category not in C.KIND:
        raise ValueError(f"Unknown category '{category}'. Known: {', '.join(C.KIND)}")
    want = normalize_text(merchant)
    groups = _group_rows(conn, scope, None)
    rows = [r for (k, _), rs in groups.items() for r in rs
            if normalize_text(k) == want or normalize_text(rs[0]["merchant"] or "") == want]
    if not rows:
        rows = [r for (k, _), rs in groups.items() for r in rs if want and want in normalize_text(rs[0]["merchant"] or "")]
    if not rows:
        raise ValueError(f"No unlabelled payments found for {merchant!r} (see uncategorised_payments).")
    pat = _pattern_for(rows) if rule else None
    extra = []
    if pat and dry_run:
        rx = re.compile(pat)
        ids = {r["id"] for r in rows}
        extra = [dict(r) for r in conn.execute("SELECT id, booking_date, amount, merchant, category, counterparty_name, "
                                               "description, remittance FROM transactions")
                 if r["id"] not in ids and rx.search(_text(r))]
    out = {"merchant": rows[0]["merchant"], "category": category, "payments": len(rows),
           "amount": r2(sum(r["amount"] for r in rows)), "rule_pattern": pat,
           "method": "rule" if pat else "per payment"}
    if dry_run:
        out["would_also_match"] = [{k: e[k] for k in ("booking_date", "amount", "merchant", "category")}
                                   for e in extra[:10]]
        out["dry_run"] = True
        return out
    if pat:
        res = C.add_rule(conn, pat, category, "any", rows[0]["merchant"])
        out["now_in_category"] = res["now_in_category"]
    else:
        for r in rows:
            C.set_category(conn, r["id"], category)
    left = conn.execute("SELECT COUNT(*) FROM transactions WHERE id IN (%s) AND category != ?" %
                        ",".join("?" * len(rows)), [r["id"] for r in rows] + [category]).fetchone()[0]
    out["not_changed"] = left  # e.g. a payment an earlier rule already claims
    return out


def label_many(conn, labels: list[dict], scope: str | None = None) -> dict:
    done, errors = [], []
    for item in labels:
        try:
            done.append(label(conn, item["merchant"], item["category"], scope))
        except (ValueError, KeyError) as e:
            errors.append({"merchant": item.get("merchant"), "error": str(e)})
    return {"labelled": done, "errors": errors}
