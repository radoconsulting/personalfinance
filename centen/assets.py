"""Investment, crypto, pension, insurance, property and loan accounts you add yourself.

Open banking only covers payment accounts, so broker accounts (DEGIRO, LYNX, Bolero...), crypto (Bitvavo, a Ledger
wallet), pension savings (pensioensparen, VAPZ, IPT / groepsverzekering), tak 21/23 contracts, your home and loans
never come in through the bank link. Here you add them yourself:

- value: what the account is worth on a date (from the broker app, a yearly pension statement, a loan table). For
  investment-type accounts centen keeps an estimate between updates: the last value plus the money you moved in or
  out since then. Price changes only show up when you enter a new value or import positions.
- payments: transfers to and from the account are recognised on your linked bank accounts by the counterparty
  account (IBAN) or a text pattern, and add up to your contributions (inleg). Contributions made before your bank
  history starts can be entered once ("inleg op datum").
- positions (optional): holdings from a broker export or typed in; their total becomes the value on that date.

Stored as accounts with source = 'asset' (never part of cash), links in asset_links, values and contributions in
balances (balance_type 'valuation' / 'contributions'), holdings in positions.
"""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import date, timedelta

from . import db
from .categorize import INVESTING_ASSET_TYPES, RULES, detect_scope, normalize_text, recategorize
from .money import clean_iban, is_valid_iban, mask_iban

ASSET = "asset"
VALUATION = "valuation"
CONTRIBUTIONS = "contributions"

TYPES = {
    "investment": "Beleggingsrekening",
    "crypto": "Crypto",
    "pension": "Pensioen",
    "insurance": "Beleggingsverzekering",
    "property": "Vastgoed",
    "asset": "Andere bezitting",
    "loan": "Lening",
}
# How old a value may get before it is flagged as stale, per type (days).
STALE_AFTER = {"investment": 45, "crypto": 45, "asset": 365, "pension": 400, "insurance": 400, "property": 730,
               "loan": 400}

_CRYPTO = re.compile(r"bitvavo|coinbase|kraken|binance|bybit|bitpanda|crypto|ledger|trezor|\bbtc\b|bitcoin")
_PENSION_TEXT = re.compile(r"pensioen|pension|vapz|plci|\bipt\b|\bric\b|groepsverzeker|levensverzeker|assurance.vie|"
                           r"tak ?2[1236]|branche ?2[136]|spaarverzeker|epargne.pension|langetermijnspar")
_SAVINGS = re.compile(r"spaar|savings|epargne|termijnrek|kasbon")
_LIFE_INSURERS = re.compile(r"\bnn\b|athora|integrale|vivium|p ?& ?v\b|allianz|\bag insurance|baloise|ethias|\baxa\b|"
                            r"federale|kbc verzekering|belfius (?:insurance|verzekering)|\bdvv\b|patronale|cardif|ageas|"
                            r"argenta assurances|nagelmackers|delta lloyd|generali|vdk verzekering")


def _broker_patterns() -> dict[str, str]:
    """Built-in broker names -> the pattern that recognises their transfers (from the categorisation rules)."""
    for cat, _d, pats, *_ in RULES:
        if cat == "Savings & investments":
            return {b.lower(): rx for b, rx in pats if b}
    return {}


def _today(today: date | None) -> date:
    return today or date.today()


def _d(s: str | None) -> date | None:
    return date.fromisoformat(s[:10]) if s else None


def r2(x) -> float | None:
    return None if x is None else round(float(x), 2)


# --- finding accounts -------------------------------------------------------------------

def _rows(conn):
    return conn.execute("SELECT * FROM accounts WHERE source=? ORDER BY name", (ASSET,)).fetchall()


def find(conn, account: str):
    """By id, name, provider, type or a part of the name ('degiro', 'pensioen')."""
    a = normalize_text(account.strip())
    rows = _rows(conn)
    exact = [r for r in rows if a in (normalize_text(r["name"]), r["id"].lower())]
    if len(exact) == 1:
        return exact[0]
    for field in ("bank", "account_type"):
        hits = [r for r in rows if a == normalize_text(r[field] or "")]
        if len(hits) == 1:
            return hits[0]
    hits = [r for r in rows if a and a in normalize_text(f"{r['name']} {r['bank'] or ''}")]
    if len(hits) == 1:
        return hits[0]
    names = ", ".join(f"{r['name']} ({TYPES.get(r['account_type'], r['account_type'])})" for r in rows) or "none yet"
    raise ValueError(f"No single investment or asset account matches '{account}'. Accounts: {names}")


def _new_id(conn, name: str) -> str:
    base = "asset:" + (re.sub(r"[^a-z0-9]+", "-", normalize_text(name)).strip("-") or "account")
    acc_id, n = base, 2
    while conn.execute("SELECT 1 FROM accounts WHERE id=?", (acc_id,)).fetchone():
        acc_id, n = f"{base}-{n}", n + 1
    return acc_id


def _resolve_counterparty(conn, iban_or_last4: str) -> str:
    raw = clean_iban(iban_or_last4) or ""
    if is_valid_iban(raw):
        return raw
    from .manual import resolve_iban
    return resolve_iban(conn, iban_or_last4)


# --- payments, values, contributions ----------------------------------------------------

def contribution_flows(conn, acc_id: str) -> tuple[list[tuple[str, float, str]], str | None]:
    """(date, money into the account, payer scope) and where they come from.

    Deposits and withdrawals in an imported broker export are the complete record when that export goes back at
    least as far as the bank data; bank transfers after the export's last date are added. Otherwise the transfers
    seen on the linked bank accounts are used."""
    bank = [(r[0], -r[1], r[2] or "personal") for r in conn.execute(
        "SELECT t.booking_date, t.amount, a.scope FROM transactions t JOIN accounts a ON a.id=t.account_id "
        "WHERE t.asset_id=? AND t.status='booked' ORDER BY t.booking_date", (acc_id,))]
    scope = (conn.execute("SELECT scope FROM accounts WHERE id=?", (acc_id,)).fetchone() or ["personal"])[0]
    broker = [(r[0], r[1], scope or "personal") for r in conn.execute(
        "SELECT date, amount_eur FROM inv_transactions WHERE account_id=? AND kind IN ('deposit', 'withdrawal') "
        "AND amount_eur IS NOT NULL ORDER BY date", (acc_id,))]
    if broker and (not bank or _d(broker[0][0]) <= _d(bank[0][0]) + timedelta(days=31)):
        until = conn.execute("SELECT MAX(date) FROM inv_transactions WHERE account_id=? "  # exports that hold deposits
                             "AND source != 'degiro_transactions'", (acc_id,)).fetchone()[0] or broker[-1][0]
        return sorted(broker + [f for f in bank if f[0] > until]), "broker export"
    return bank, ("bank transfers" if bank else None)


def _flows(conn, acc_id: str) -> list[tuple[str, float, str]]:
    return contribution_flows(conn, acc_id)[0]


def _cum(flows, upto: date | None, after: date | None = None) -> float:
    return sum(f[1] for f in flows if (upto is None or _d(f[0]) <= upto) and (after is None or _d(f[0]) > after))


def _entries(conn, acc_id: str, kind: str) -> list[tuple[str, float]]:
    return [(r[0], r[1]) for r in conn.execute(
        "SELECT as_of, amount FROM balances WHERE account_id=? AND balance_type=? ORDER BY as_of", (acc_id, kind))]


def _value_at(acc, values, flows, t: date) -> float | None:
    past = [v for v in values if _d(v[0]) <= t]
    if not past:
        return None
    vd, v = past[-1]
    if acc["account_type"] in INVESTING_ASSET_TYPES:
        v += _cum(flows, t, _d(vd))
    return v


def _contrib_at(contribs, flows, t: date | None) -> float | None:
    """Contributions up to t: your entered figure on its date, moved along with the payments seen in the bank data."""
    past = [c for c in contribs if t is None or _d(c[0]) <= t] or contribs[:1]
    if past:
        cd, c = past[-1]
        return c - _cum(flows, _d(cd)) + _cum(flows, t)
    if not flows:
        return None
    return _cum(flows, t)


def _history_start(conn) -> str | None:
    """First day of bank history on your own linked or imported accounts."""
    return conn.execute("SELECT MIN(t.booking_date) FROM transactions t JOIN accounts a ON a.id=t.account_id "
                        "WHERE a.source NOT IN ('asset', 'manual')").fetchone()[0]


def _newest_data(conn) -> str | None:
    return conn.execute("SELECT MAX(booking_date) FROM transactions").fetchone()[0]


def describe(conn, acc_id: str, today: date | None = None, newest: str | None = None) -> dict:
    today = _today(today)
    acc = conn.execute("SELECT * FROM accounts WHERE id=?", (acc_id,)).fetchone()
    typ = acc["account_type"]
    flows, source = contribution_flows(conn, acc_id)
    values = _entries(conn, acc_id, VALUATION)
    contribs = [] if source == "broker export" else _entries(conn, acc_id, CONTRIBUTIONS)
    out = {"name": acc["name"], "type": typ, "type_label": TYPES.get(typ, typ), "provider": acc["bank"],
           "scope": acc["scope"] or "personal"}
    value = _value_at(acc, values, flows, today)
    if values:
        vd, v = values[-1]
        since = [f for f in flows if _d(f[0]) > _d(vd)]
        age = (today - _d(vd)).days
        newest = newest if newest is not None else _newest_data(conn)
        out.update({
            "value": r2(value), "value_entered": {"amount": r2(v), "date": vd},
            "value_as_of": max(vd, newest or vd) if typ in INVESTING_ASSET_TYPES and since else vd,
            "estimated": bool(since) and typ in INVESTING_ASSET_TYPES,
            "moved_since_value": r2(sum(f[1] for f in since)), "payments_since_value": len(since),
            "days_since_value": age, "stale": age > STALE_AFTER.get(typ, 365),
        })
    else:
        out.update({"value": None, "value_entered": None})
    if typ == "loan":
        out["outstanding"] = out.pop("value")
        out["paid_since_value"] = out.pop("moved_since_value", None)
        out["paid_total_seen"] = r2(sum(f[1] for f in flows)) if flows else None
        from . import loans
        plan = loans.describe(conn, acc_id, today)
        if plan:  # repayment plan known: the amount owed follows the schedule instead of staying at the statement
            out["outstanding"] = plan["outstanding_now"]
            out["estimated"] = out["outstanding"] != plan["statement"]["outstanding"]
            out["value_as_of"] = today.isoformat()
            out["stale"] = False
            out["plan"] = plan
    else:
        c = _contrib_at(contribs, flows, None)
        by_payer = defaultdict(float)
        for f in flows:
            by_payer[f[2]] += f[1]
        out["contributions"] = r2(c)
        out["contributions_source"] = "entered" if contribs else source
        out["contributions_entered"] = {"amount": r2(contribs[-1][1]), "date": contribs[-1][0]} if contribs else None
        if len(by_payer) > 1 or (by_payer and next(iter(by_payer)) != out["scope"]):
            out["contributions_by_payer"] = {k: r2(v) for k, v in by_payer.items()}
        # Contributions from bank transfers only cover the bank history. When the account already had a value before
        # that history starts, earlier deposits are missing and
        # value − contributions would count them as return (r22: DEGIRO showed +330 %). Then no return is given until
        # the total contributions are entered once, or a full broker history is imported.
        complete = True
        if not contribs and source != "broker export" and flows:
            hist = _history_start(conn)
            first_val = values[0][0] if values else None
            if hist and first_val and _d(first_val) < _d(hist):
                complete = False
                out["contributions_since"] = hist
            elif hist and (_d(flows[0][0]) - _d(hist)).days <= 31:
                # payments already run when the history starts: there may well be older ones. The return is still
                # shown, flagged, until the total contributions are entered once.
                out["contributions_uncertain"] = True
                out["contributions_since"] = hist
        out["contributions_complete"] = complete
        if out["value"] is not None and c is not None and complete:
            out["gain"] = r2(out["value"] - c)
            out["gain_pct"] = round((out["value"] - c) / c, 4) if c > 0 else None
        if out["value"] is None and c is not None and typ in INVESTING_ASSET_TYPES:
            out["value_basis"] = "contributions"  # no value yet: count what went in
    out["payments"] = len(flows)
    if flows:
        out["first_payment"], out["last_payment"] = flows[0][0], flows[-1][0]
    pos = conn.execute("SELECT as_of, COUNT(*), SUM(value) FROM positions WHERE account_id=? GROUP BY as_of "
                       "ORDER BY as_of DESC LIMIT 1", (acc_id,)).fetchone()
    if pos:
        out["positions"] = {"as_of": pos[0], "count": pos[1], "value": r2(pos[2])}
    out["recognised_by"] = [
        {"account": r["iban_masked"]} if r["kind"] == "iban" else {"pattern": r["pattern"]}
        for r in conn.execute("SELECT kind, iban_masked, pattern FROM asset_links WHERE account_id=? ORDER BY id",
                              (acc_id,))]
    return out


def counted_value(d: dict) -> float:
    """What an account adds to your net worth: its value, else (investments only) your contributions."""
    if d["type"] == "loan":
        return -(d.get("outstanding") or 0)
    if d.get("value") is not None:
        return d["value"]
    if d.get("value_basis") == "contributions":
        return max(0.0, d.get("contributions") or 0)
    return 0.0


def list_accounts(conn, today: date | None = None) -> list[dict]:
    newest = _newest_data(conn)
    return [describe(conn, r["id"], today, newest) for r in _rows(conn)]


# --- payments to brokers that aren't an account here yet --------------------------------

def unassigned(conn) -> list[dict]:
    """Money moved to brokers / investment accounts (Savings & investments) not linked to an account here."""
    out = defaultdict(lambda: {"net": 0.0, "n": 0, "first": None, "last": None, "ibans": set(), "scopes": defaultdict(int)})
    for r in conn.execute(
            "SELECT t.merchant, t.amount, t.booking_date, t.counterparty_iban, a.scope FROM transactions t "
            "JOIN accounts a ON a.id=t.account_id WHERE t.category='Savings & investments' AND t.asset_id IS NULL "
            "AND t.status='booked'"):
        v = out[r[0] or "Unknown"]
        v["net"] -= r[1]
        v["n"] += 1
        v["first"] = min(v["first"] or r[2], r[2])
        v["last"] = max(v["last"] or r[2], r[2])
        if r[3]:
            v["ibans"].add(r[3])
        v["scopes"][r[4] or "personal"] += 1
    res = []
    for name, v in out.items():
        res.append({"destination": name, "net_deposited": r2(v["net"]), "payments": v["n"], "since": v["first"],
                    "last": v["last"], "accounts": sorted(mask_iban(i) for i in v["ibans"]),
                    "scope": max(v["scopes"], key=v["scopes"].get),
                    "type_guess": "crypto" if _CRYPTO.search(normalize_text(name)) else "investment"})
    res.sort(key=lambda x: -abs(x["net_deposited"]))
    return res


def _unassigned_ibans(conn, destination: str) -> list[str]:
    return sorted({r[0] for r in conn.execute(
        "SELECT DISTINCT counterparty_iban FROM transactions WHERE category='Savings & investments' "
        "AND asset_id IS NULL AND merchant=? AND counterparty_iban IS NOT NULL", (destination,))})


def suggestions(conn) -> list[dict]:
    """Things that look like investment or pension accounts, from your payments."""
    out = []
    for u in unassigned(conn):
        if abs(u["net_deposited"]) < 1 and u["payments"] < 3:
            continue
        if _SAVINGS.search(normalize_text(u["destination"])):
            continue  # a savings account: add it with add_manual_account (see unlinked_own_accounts)
        out.append({**u, "why": "transfers to a broker or investment account",
                    "add_with": {"name": u["destination"], "asset_type": u["type_guess"], "scope": u["scope"]}})
    # Insurance premiums that look like a pension or savings contract (IPT, groepsverzekering, tak 21/23, VAPZ...).
    agg = defaultdict(lambda: {"n": 0, "big": 0, "total": 0.0, "last": None, "msg": None, "scopes": set(),
                               "text": ""})
    for r in conn.execute(
            "SELECT t.merchant, t.amount, t.booking_date, t.counterparty_name, t.description, t.remittance, a.scope "
            "FROM transactions t JOIN accounts a ON a.id=t.account_id WHERE t.category='Insurance' "
            "AND t.amount < 0 AND t.asset_id IS NULL AND t.status='booked'"):
        v = agg[r[0] or "Unknown"]
        v["n"] += 1
        v["total"] -= r[1]
        v["big"] += -r[1] >= 1000
        if not v["last"] or r[2] > v["last"]:
            v["last"], v["msg"] = r[2], (r[5] or r[4] or "")[:90]
        v["scopes"].add(r[6] or "personal")
        v["text"] += " " + normalize_text(" ".join(x or "" for x in (r[3], r[4], r[5])))
    from .money import mask_ibans_in_text
    for merchant, v in agg.items():
        hay = normalize_text(merchant) + v["text"]
        if not (_PENSION_TEXT.search(hay) or (v["big"] >= 2 and _LIFE_INSURERS.search(hay))):
            continue
        out.append({"destination": merchant, "payments": v["n"], "paid_total": r2(v["total"]), "last": v["last"],
                    "last_message": mask_ibans_in_text(v["msg"]), "paid_from": sorted(v["scopes"]),
                    "type_guess": "pension", "why": "insurance premiums that may be a pension or savings contract "
                                                    "(IPT, groepsverzekering, VAPZ, tak 21/23): check with the user",
                    "add_with": {"name": merchant, "asset_type": "pension", "match": [merchant]}})
    return out


# --- changing accounts ------------------------------------------------------------------

def _add_links(conn, acc_id: str, ibans=(), patterns=()) -> None:
    have = {(r[0], r[1] or r[2]) for r in conn.execute("SELECT kind, iban, pattern FROM asset_links WHERE account_id=?",
                                                       (acc_id,))}
    for i in ibans:
        if ("iban", i) in have:
            continue
        other = conn.execute("SELECT a.name FROM asset_links l JOIN accounts a ON a.id=l.account_id WHERE l.iban=? "
                             "AND l.account_id != ?", (i, acc_id)).fetchone()
        if other:
            raise ValueError(f"{mask_iban(i)} already belongs to {other[0]}.")
        if conn.execute("SELECT 1 FROM accounts WHERE iban=?", (i,)).fetchone():
            raise ValueError(f"{mask_iban(i)} is one of your bank or savings accounts, not an investment account.")
        conn.execute("INSERT INTO asset_links(account_id, kind, iban, iban_masked) VALUES (?,?,?,?)",
                     (acc_id, "iban", i, mask_iban(i)))
    for p in patterns:
        p = (p or "").strip()
        if p and ("match", p) not in have:
            conn.execute("INSERT INTO asset_links(account_id, kind, pattern) VALUES (?,?,?)", (acc_id, "match", p))


def _check_type(asset_type: str) -> str:
    t = (asset_type or "").strip().lower()
    aliases = {"broker": "investment", "beleggingen": "investment", "effecten": "investment", "fund": "investment",
               "funds": "investment", "fondsen": "investment", "pensioensparen": "pension", "vapz": "pension",
               "ipt": "pension", "groepsverzekering": "pension", "tak21": "insurance", "tak23": "insurance",
               "woning": "property", "real_estate": "property", "vastgoed": "property", "lening": "loan",
               "mortgage": "loan", "hypotheek": "loan", "other": "asset"}
    t = aliases.get(t, t)
    if t not in TYPES:
        raise ValueError(f"asset_type must be one of {', '.join(TYPES)}")
    return t


def _set(conn, acc_id: str, kind: str, amount: float, when: str | None, today: date | None = None) -> None:
    d = date.fromisoformat(when[:10]) if when else _today(today)
    db.set_balance(conn, acc_id, d.isoformat(), kind, round(float(amount), 2))


def add_account(conn, name: str, asset_type: str, value: float | None = None, value_date: str | None = None,
                provider: str | None = None, scope: str | None = None, match: list[str] | None = None,
                counterparty_accounts: list[str] | None = None, contributions: float | None = None,
                contributions_date: str | None = None, today: date | None = None) -> dict:
    name = (name or "").strip()
    if not name:
        raise ValueError("Give the account a name, e.g. 'DEGIRO' or 'Pensioensparen AG'.")
    typ = _check_type(asset_type)
    if scope not in (None, "personal", "business"):
        raise ValueError("scope must be 'personal' or 'business'")
    if conn.execute("SELECT 1 FROM accounts WHERE source=? AND lower(name)=lower(?)", (ASSET, name)).fetchone():
        raise ValueError(f"There is already an account called '{name}'; update it instead.")
    ibans = [_resolve_counterparty(conn, x) for x in (counterparty_accounts or [])]
    patterns = list(match or [])
    auto = None
    if not ibans and not patterns:  # an unassigned broker with the same name: take over its transfers
        key = normalize_text(provider or name)
        for u in unassigned(conn):
            dn = normalize_text(u["destination"])
            if dn and dn != "unknown" and (dn == key or re.search(rf"\b{re.escape(dn)}\b", key)):
                auto = u
                ibans = _unassigned_ibans(conn, u["destination"])
                patterns = [_broker_patterns().get(dn) or re.escape(dn)]
                provider = provider or u["destination"]
                scope = scope or u["scope"]
                break
    acc_id = _new_id(conn, name)
    if not scope:
        scope = "business" if detect_scope(name) == "business" else "personal"
    db.upsert_account(conn, id=acc_id, source=ASSET, bank=provider, name=name, account_type=typ, scope=scope)
    conn.execute("UPDATE accounts SET scope=? WHERE id=?", (scope, acc_id))
    _add_links(conn, acc_id, ibans, patterns)
    if value is not None:
        _set(conn, acc_id, VALUATION, abs(value) if typ == "loan" else value, value_date, today)
    if contributions is not None:
        _set(conn, acc_id, CONTRIBUTIONS, contributions, contributions_date or value_date, today)
    conn.commit()
    recategorize(conn)  # payments to it now count as savings / investments
    out = describe(conn, acc_id, today)
    if auto:
        out["took_over"] = f"transfers to {auto['destination']} ({auto['payments']} payments) seen on your bank accounts"
    if not out["recognised_by"] and value is None:
        out["warning"] = "No value and no payments linked yet: give a value, or patterns / accounts to recognise payments."
    elif out["recognised_by"] and not out["payments"]:
        out["note"] = "No payments matched yet; they will be picked up as they arrive."
    return out


def update_account(conn, account: str, name: str | None = None, provider: str | None = None,
                   scope: str | None = None, asset_type: str | None = None, add_match: list[str] | None = None,
                   remove_match: list[str] | None = None, add_counterparty_accounts: list[str] | None = None,
                   remove_counterparty_accounts: list[str] | None = None, contributions: float | None = None,
                   contributions_date: str | None = None, today: date | None = None) -> dict:
    acc = find(conn, account)
    if scope not in (None, "personal", "business"):
        raise ValueError("scope must be 'personal' or 'business'")
    if name:
        conn.execute("UPDATE accounts SET name=?, updated_at=datetime('now') WHERE id=?", (name.strip(), acc["id"]))
    if provider is not None:
        conn.execute("UPDATE accounts SET bank=? WHERE id=?", (provider or None, acc["id"]))
    if scope:
        conn.execute("UPDATE accounts SET scope=? WHERE id=?", (scope, acc["id"]))
    if asset_type:
        conn.execute("UPDATE accounts SET account_type=? WHERE id=?", (_check_type(asset_type), acc["id"]))
    _add_links(conn, acc["id"], [_resolve_counterparty(conn, x) for x in (add_counterparty_accounts or [])],
               add_match or [])
    for p in remove_match or []:
        conn.execute("DELETE FROM asset_links WHERE account_id=? AND kind='match' AND lower(pattern)=lower(?)",
                     (acc["id"], p.strip()))
    for x in remove_counterparty_accounts or []:
        digits = "".join(ch for ch in x if ch.isdigit())[-4:]
        conn.execute("DELETE FROM asset_links WHERE account_id=? AND kind='iban' AND (iban=? OR iban LIKE ?)",
                     (acc["id"], clean_iban(x), f"%{digits}" if len(digits) == 4 else "\x00"))
    if contributions is not None:
        _set(conn, acc["id"], CONTRIBUTIONS, contributions, contributions_date, today)
    conn.commit()
    recategorize(conn)
    return describe(conn, acc["id"], today)


def set_value(conn, account: str, value: float, value_date: str | None = None, today: date | None = None) -> dict:
    acc = find(conn, account)
    _set(conn, acc["id"], VALUATION, abs(value) if acc["account_type"] == "loan" else value, value_date, today)
    from . import depreciation  # a car with a schedule continues from the value entered
    depreciation.reanchor(conn, acc["id"], value, (value_date or _today(today).isoformat()))
    conn.commit()
    return describe(conn, acc["id"], today)


def remove(conn, account: str, today: date | None = None) -> dict:
    acc = find(conn, account)
    for table in ("asset_links", "positions", "balances", "inv_transactions"):
        conn.execute(f"DELETE FROM {table} WHERE account_id=?", (acc["id"],))
    conn.execute("DELETE FROM accounts WHERE id=?", (acc["id"],))
    conn.commit()
    recategorize(conn)
    return {"removed": acc["name"], "type": acc["account_type"]}


# --- positions --------------------------------------------------------------------------

POSITION_FIELDS = ("name", "isin", "symbol", "asset_class", "quantity", "price", "currency", "value_local", "value",
                   "cost")


def set_positions(conn, account: str, positions: list[dict], as_of: str | None = None, update_value: bool = True,
                  source: str = "manual", total_value: float | None = None, today: date | None = None) -> dict:
    """Replace the holdings of an account on a date. value is in EUR; their sum becomes the account's value."""
    acc = find(conn, account)
    if acc["account_type"] in ("loan", "property"):
        raise ValueError("Positions are for investment, crypto, pension and insurance accounts.")
    d = (date.fromisoformat(as_of[:10]) if as_of else _today(today)).isoformat()
    rows = []
    for p in positions:
        p = {k: p.get(k) for k in POSITION_FIELDS}
        if not p["name"]:
            p["name"] = p["symbol"] or p["isin"]
        if not p["name"]:
            raise ValueError("Every position needs a name, symbol or ISIN.")
        if p["value"] is None and p["quantity"] is not None and p["price"] is not None and \
                (p["currency"] or "EUR").upper() == "EUR":
            p["value"] = float(p["quantity"]) * float(p["price"])
        if not p["asset_class"]:
            p["asset_class"] = guess_class(p["name"], p["isin"], acc["account_type"])
        rows.append(p)
    missing = [p["name"] for p in rows if p["value"] is None]
    conn.execute("DELETE FROM positions WHERE account_id=? AND as_of=?", (acc["id"], d))
    conn.executemany(
        f"INSERT INTO positions(account_id, as_of, {', '.join(POSITION_FIELDS)}, source) "
        f"VALUES (?,?,{','.join('?' * len(POSITION_FIELDS))},?)",
        [(acc["id"], d, *[p[k] for k in POSITION_FIELDS], source) for p in rows])
    total = total_value if total_value is not None else sum(p["value"] or 0 for p in rows)
    value_set = False
    if update_value and rows and (not missing or total_value is not None):
        _set(conn, acc["id"], VALUATION, total, d)
        value_set = True
    conn.commit()
    out = {"account": acc["name"], "as_of": d, "positions": len(rows), "total_value": r2(total),
           "value_updated": value_set}
    if missing and total_value is None:
        out["warning"] = (f"No EUR value for {', '.join(missing[:5])}: give value in EUR for every position, "
                          "so the account value wasn't changed.")
    out["account_now"] = describe(conn, acc["id"], today)
    return out


def import_positions(conn, account: str, path: str, as_of: str | None = None, update_value: bool = True,
                     today: date | None = None) -> dict:
    """Read a broker export (DEGIRO Portfolio.csv, LYNX / IBKR activity statement CSV, or a simple table)."""
    from .positions import parse_file
    res = parse_file(path)
    out = set_positions(conn, account, res["positions"], as_of or res["as_of"], update_value, source=res["format"],
                        total_value=res["total_value"], today=today)
    out["format"] = res["format"]
    if res["warnings"]:
        out["file_warnings"] = res["warnings"][:10]
    return out


_ETF = re.compile(r"\betf\b|ucits|ishares|vanguard|xtrackers|amundi|spdr|lyxor|invesco|wisdomtree|\bvaneck\b|"
                  r"\bjpm\b.*ucits|\btracker\b")
_FUND = re.compile(r"\bfund\b|\bfonds\b|sicav|\bbevek\b|world funds|axa b fund|\bacc\b.*\bcap\b|\bcap\b|\bdis\b")
_CASH = re.compile(r"^cash\b|cash fund|geldmarkt|money market|\bliquiditeit|\bflatex cash|\bcash &")
_BOND = re.compile(r"\bbond|obligati|treasury|\bgovt\b|aggregate")
_COINS = {"btc", "eth", "sol", "ada", "xrp", "dot", "doge", "ltc", "link", "matic", "pol", "avax", "usdt", "usdc", "bnb",
          "atom", "xlm", "algo", "trx", "shib", "near", "arb", "op", "sui", "apt"}


def guess_class(name: str | None, isin: str | None = None, account_type: str | None = None) -> str:
    n = normalize_text(name or "")
    if _CASH.search(n):
        return "cash"
    if account_type == "crypto" or n in _COINS or re.search(r"bitcoin|ethereum|solana|cardano|ripple|crypto", n):
        return "crypto"
    if _ETF.search(n):
        return "etf"
    if _BOND.search(n):
        return "bond"
    if _FUND.search(n) or account_type in ("pension", "insurance"):
        return "fund"
    return "equity" if isin or n else "other"


def latest_positions(conn) -> list[dict]:
    out = []
    for acc in _rows(conn):
        d = conn.execute("SELECT MAX(as_of) FROM positions WHERE account_id=?", (acc["id"],)).fetchone()[0]
        if not d:
            continue
        for r in conn.execute("SELECT * FROM positions WHERE account_id=? AND as_of=? ORDER BY value DESC",
                              (acc["id"], d)):
            out.append({"account": acc["name"], "as_of": d, **{k: r[k] for k in POSITION_FIELDS}})
    for p in out:
        for k in ("value", "value_local", "cost", "price"):
            p[k] = r2(p[k])
    return out


# --- totals and history -----------------------------------------------------------------

def summary(conn, today: date | None = None) -> dict:
    today = _today(today)
    accounts = list_accounts(conn, today)
    # "asset" (a car, a watch) is a possession, not an investment: it counts in the net worth, not in the
    # investments' value and return (r19).
    inv = [a for a in accounts if a["type"] in INVESTING_ASSET_TYPES and a["type"] != "asset"]
    things = [a for a in accounts if a["type"] == "asset"]
    loans = [a for a in accounts if a["type"] == "loan"]
    prop = [a for a in accounts if a["type"] == "property"]
    un = unassigned(conn)
    by_type, by_scope = defaultdict(float), defaultdict(float)
    for a in inv + prop + things:
        by_type[a["type"]] += counted_value(a)
        by_scope[a["scope"]] += counted_value(a)
    valued = [a for a in inv if a.get("value") is not None and a.get("contributions") is not None
              and a.get("contributions_complete", True)]
    incomplete = [a["name"] for a in inv if a.get("value") is not None and not a.get("contributions_complete", True)]
    contrib = sum(a["contributions"] for a in valued)
    gain = sum(a["gain"] for a in valued)
    un_pos = sum(max(0.0, u["net_deposited"]) for u in un)
    for u in un:
        by_scope[u["scope"]] += max(0.0, u["net_deposited"])
    values = [a for a in inv + prop + things + loans if a.get("value_entered")]
    totals = {
        "investments_value": r2(sum(counted_value(a) for a in inv)),
        "investments_contributions": r2(sum(a["contributions"] or 0 for a in inv)),
        "gain_on_valued_accounts": r2(gain) if valued else None,
        "gain_pct": round(gain / contrib, 4) if valued and contrib > 0 else None,
        "gain_excluded": incomplete,
        "gain_uncertain": [a["name"] for a in valued if a.get("contributions_uncertain")],
        "history_start": _history_start(conn),
        "property": r2(sum(counted_value(a) for a in prop)),
        "possessions": r2(sum(counted_value(a) for a in things)),
        "loans_outstanding": r2(sum(a.get("outstanding") or 0 for a in loans)),
        "not_added_net_deposited": r2(un_pos),
        "by_type": {k: r2(v) for k, v in by_type.items()},
        "by_scope": {k: r2(v) for k, v in by_scope.items()},
        "business_pension": r2(sum(counted_value(a) for a in inv if a["type"] == "pension" and a["scope"] == "business")),
        "oldest_value": min((a["value_entered"]["date"] for a in values), default=None),
        "accounts_without_value": [a["name"] for a in inv + prop + things + loans if not a.get("value_entered")],
    }
    totals["assets_net"] = r2((totals["investments_value"] or 0) + (totals["property"] or 0)
                              + (totals["possessions"] or 0) + un_pos
                              - (totals["loans_outstanding"] or 0))
    return {"accounts": [a for a in accounts if a["type"] != "loan"], "loans": loans, "not_added": un,
            "totals": totals}


def timeline(conn, today: date | None = None) -> dict:
    """Month-end contributions (all investment-type accounts and brokers not added yet) and, from the first value
    you entered, the estimated value of the same set."""
    from .analytics import add_months
    today = _today(today)
    accs = [r for r in _rows(conn) if r["account_type"] in INVESTING_ASSET_TYPES]
    data = {}
    for a in accs:
        fl, src = contribution_flows(conn, a["id"])
        data[a["id"]] = (a, fl, _entries(conn, a["id"], VALUATION),
                         [] if src == "broker export" else _entries(conn, a["id"], CONTRIBUTIONS))
    loose = [(r[0], -r[1]) for r in conn.execute(
        "SELECT booking_date, amount FROM transactions WHERE category='Savings & investments' AND asset_id IS NULL "
        "AND status='booked' ORDER BY booking_date")]
    starts = [f[0] for _, fl, _, _ in data.values() for f in fl] + [x[0] for x in loose] + \
             [v[0] for _, _, vs, cs in data.values() for v in vs + cs]
    if not starts:
        return {"months": [], "contributions": [], "value": []}
    d = date.fromisoformat(min(starts)[:7] + "-01")
    months, contrib, value = [], [], []
    # The value line starts once every account that has values has its first one (before that it would mix values
    # with contributions); accounts without any value count at their contributions throughout, as in net worth.
    first_val = max((vs[0][0] for _, _, vs, _ in data.values() if vs), default=None)
    while d <= today:
        end = min(add_months(d, 1) - timedelta(days=1), today)
        months.append(d.isoformat()[:7])
        c_tot, v_tot = 0.0, 0.0
        for a, fl, vs, cs in data.values():
            c = _contrib_at(cs, fl, end) or 0.0
            v = _value_at(a, vs, fl, end)
            c_tot += c
            v_tot += v if v is not None else max(0.0, c)
        lc = sum(x[1] for x in loose if _d(x[0]) <= end)
        contrib.append(r2(c_tot + lc))
        value.append(r2(v_tot + max(0.0, lc)) if first_val and end >= _d(first_val) else None)
        d = add_months(d, 1)
    return {"months": months, "contributions": contrib, "value": value, "first_value": first_val}
