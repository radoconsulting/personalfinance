"""Accounts the bank doesn't share through open banking (PSD2 only covers payment accounts, so regulated savings
accounts are usually missing). You enter the balance once; centen then keeps an estimate by adding every transfer to
or from that IBAN seen on your linked accounts. Interest paid on the savings account itself stays invisible until
you enter a new balance.
"""
from __future__ import annotations

from datetime import date

from . import db
from .categorize import detect_scope, recategorize
from .money import clean_iban, is_valid_iban, mask_iban

MANUAL = "manual"
BALANCE_TYPE = "manual"

# Belgian bank codes (IBAN positions 5-7) for banks whose accounts often show up here unlinked.
_BANK_CODES = [((645, 645), "Bank Van Breda"), ((750, 774), "Crelan"), ((103, 108), "Crelan"), ((859, 860), "Crelan"),
               ((730, 731), "KBC"), ((733, 741), "KBC"), ((743, 749), "KBC"), ((50, 99), "Belfius"),
               ((1, 49), "BNP Paribas Fortis"), ((310, 399), "ING"), ((979, 980), "Argenta")]


def guess_bank(conn, iban: str) -> str | None:
    if not iban or not iban.startswith("BE") or len(iban) < 7:
        return None
    code = iban[4:7]
    for r in conn.execute("SELECT iban, bank FROM accounts WHERE iban LIKE 'BE%' AND bank IS NOT NULL"):
        if r[0][4:7] == code:
            return r[1]
    n = int(code)
    return next((name for (lo, hi), name in _BANK_CODES if lo <= n <= hi), None)


def resolve_iban(conn, iban_or_last4: str) -> str:
    """A full IBAN, or the last 4 digits of an IBAN that appears as a counterparty in your transactions."""
    raw = clean_iban(iban_or_last4) or ""
    if is_valid_iban(raw):
        return raw
    digits = "".join(ch for ch in iban_or_last4 if ch.isdigit())
    if len(digits) != 4:
        raise ValueError("Give the full IBAN or its last 4 digits.")
    hits = sorted({r[0] for r in conn.execute(
        "SELECT DISTINCT counterparty_iban FROM transactions WHERE counterparty_iban LIKE ?", (f"%{digits}",))})
    if not hits:
        raise ValueError(f"No transfers to or from an account ending in {digits}: give the full IBAN.")
    if len(hits) > 1:
        raise ValueError(f"Several accounts end in {digits} ({', '.join(mask_iban(h) for h in hits)}): give the full IBAN.")
    return hits[0]


def _find(conn, account: str):
    a = account.strip()
    rows = conn.execute("SELECT * FROM accounts WHERE source=?", (MANUAL,)).fetchall()
    digits = "".join(ch for ch in a if ch.isdigit())
    for r in rows:
        if a.lower() in ((r["name"] or "").lower(), r["id"].lower()) or \
                (len(digits) == 4 and (r["iban"] or "").endswith(digits)) or clean_iban(a) == r["iban"]:
            return r
    hits = [r for r in rows if a.lower() in f"{r['name']} {r['bank']}".lower()]
    if len(hits) == 1:
        return hits[0]
    raise ValueError(f"No single manual account matches '{account}'. Manual accounts: "
                     + (", ".join(f"{r['name']} ({r['bank']}, {r['iban_masked']})" for r in rows) or "none"))


def add_account(conn, iban_or_last4: str, balance: float, balance_date: str | None = None, name: str | None = None,
                bank: str | None = None, scope: str | None = None, account_type: str = "savings") -> dict:
    iban = resolve_iban(conn, iban_or_last4)
    if iban in db.asset_ibans(conn):
        raise ValueError(f"{mask_iban(iban)} is linked to an investment account (see list_assets).")
    existing = conn.execute("SELECT id, source, link_id FROM accounts WHERE iban=?", (iban,)).fetchone()
    if existing and (existing["source"] != MANUAL or existing["link_id"]):
        raise ValueError(f"{mask_iban(iban)} is already a linked or imported account; its balance comes from the bank.")
    acc_id = existing["id"] if existing else f"acc:{iban}"
    if not name:
        r = conn.execute("SELECT counterparty_name, COUNT(*) n FROM transactions WHERE counterparty_iban=? "
                         "AND counterparty_name IS NOT NULL GROUP BY 1 ORDER BY n DESC LIMIT 1", (iban,)).fetchone()
        name = r[0] if r else f"Spaarrekening {mask_iban(iban)}"
        from .categorize import _tokens
        same = [x[0] for x in conn.execute("SELECT name FROM accounts WHERE source != ? AND name IS NOT NULL", (MANUAL,))
                if _tokens(x[0]) == _tokens(name)]
        name = same[0] if same else name  # "Acme Consulting bv" -> "Acme Consulting BV", as on the linked account
    bank = bank or guess_bank(conn, iban)
    if scope not in (None, "personal", "business"):
        raise ValueError("scope must be 'personal' or 'business'")
    scope = scope or _scope_from_holder(conn, name)
    db.upsert_account(conn, id=acc_id, source=MANUAL, bank=bank, name=name, iban=iban, account_type=account_type,
                      scope=scope)
    if scope:
        conn.execute("UPDATE accounts SET scope=? WHERE id=?", (scope, acc_id))
    _set(conn, acc_id, balance, balance_date)
    conn.commit()
    recategorize(conn)  # transfers to this IBAN are now moves between your own accounts
    return describe(conn, acc_id)


def _scope_from_holder(conn, name: str) -> str:
    if detect_scope(name) == "business":
        return "business"
    from .categorize import _tokens
    toks = _tokens(name)
    for r in conn.execute("SELECT name, scope FROM accounts WHERE source != ?", (MANUAL,)):
        if _tokens(r["name"]) == toks:
            return r["scope"] or "personal"
    return "personal"


def _set(conn, acc_id: str, balance: float, balance_date: str | None):
    d = date.fromisoformat(balance_date) if balance_date else date.today()
    db.set_balance(conn, acc_id, d.isoformat(), BALANCE_TYPE, round(float(balance), 2))


def set_balance(conn, account: str, balance: float, balance_date: str | None = None) -> dict:
    r = _find(conn, account)
    _set(conn, r["id"], balance, balance_date)
    conn.commit()
    return describe(conn, r["id"])


def remove(conn, account: str) -> dict:
    r = _find(conn, account)
    conn.execute("DELETE FROM balances WHERE account_id=?", (r["id"],))
    conn.execute("DELETE FROM accounts WHERE id=?", (r["id"],))
    conn.commit()
    recategorize(conn)
    return {"removed": r["name"], "account": r["iban_masked"]}


def estimate(conn, acc) -> dict | None:
    """Last balance you entered plus the transfers seen since then on your linked accounts."""
    if acc["source"] != MANUAL or acc["link_id"]:
        return None
    m = conn.execute("SELECT amount, as_of FROM balances WHERE account_id=? AND balance_type=? ORDER BY as_of DESC "
                     "LIMIT 1", (acc["id"], BALANCE_TYPE)).fetchone()
    if not m:
        return None
    moved, count = conn.execute(
        "SELECT COALESCE(SUM(-amount), 0), COUNT(*) FROM transactions WHERE counterparty_iban=? AND booking_date > ? "
        "AND status='booked' AND account_id != ?", (acc["iban"], m["as_of"], acc["id"])).fetchone()
    newest = conn.execute("SELECT MAX(booking_date) FROM transactions WHERE account_id IN "
                          "(SELECT id FROM accounts WHERE source != ?)", (MANUAL,)).fetchone()[0]
    return {"amount": round(m["amount"] + moved, 2), "as_of": max(m["as_of"], newest or m["as_of"]),
            "entered": {"amount": m["amount"], "date": m["as_of"]}, "transfers_since": count,
            "moved_since": round(moved, 2)}


def describe(conn, acc_id: str) -> dict:
    acc = conn.execute("SELECT * FROM accounts WHERE id=?", (acc_id,)).fetchone()
    e = estimate(conn, acc) or {}
    return {"name": acc["name"], "bank": acc["bank"], "account": acc["iban_masked"], "type": acc["account_type"],
            "scope": acc["scope"], "estimated_balance": e.get("amount"), "balance_entered": e.get("entered"),
            "transfers_since_entry": e.get("transfers_since"), "moved_since_entry": e.get("moved_since")}


def list_accounts(conn) -> list[dict]:
    return [describe(conn, r[0]) for r in conn.execute("SELECT id FROM accounts WHERE source=? ORDER BY name", (MANUAL,))]


def suggestions(conn) -> list[dict]:
    """Belgian accounts you move money to or from as if they were your own, but that aren't linked or added."""
    known = db.own_ibans(conn) | db.asset_ibans(conn)
    rows = conn.execute(
        "SELECT counterparty_iban iban, MAX(counterparty_name) name, COUNT(*) n, "
        "SUM(CASE WHEN amount < 0 THEN -amount ELSE 0 END) out_, SUM(CASE WHEN amount > 0 THEN amount ELSE 0 END) in_, "
        "MIN(booking_date) first, MAX(booking_date) last, GROUP_CONCAT(DISTINCT category) cats "
        "FROM transactions WHERE category IN ('Own accounts', 'Savings & investments') AND status='booked' "
        "AND counterparty_iban LIKE 'BE%' GROUP BY counterparty_iban ORDER BY (out_ + in_) DESC").fetchall()
    out = []
    for r in rows:
        if r["iban"] in known:
            continue
        out.append({"account": mask_iban(r["iban"]), "last4": r["iban"][-4:], "holder": r["name"],
                    "bank_guess": guess_bank(conn, r["iban"]), "transfers": r["n"],
                    "you_sent_to_it": round(r["out_"], 2), "you_received_from_it": round(r["in_"], 2),
                    "first": r["first"], "last": r["last"], "now_counted_as": r["cats"].split(",")})
    return out
