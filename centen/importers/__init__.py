"""File importers: CSV exports of Belgian banks and ISO 20022 CAMT.053 statements."""
from __future__ import annotations

import re
import sqlite3
from collections import Counter
from pathlib import Path

from .. import db
from ..categorize import Context, enrich, recategorize
from ..money import clean_iban

# Belgian bank protocol codes (IBAN positions 5-7) for a friendly bank label.
_BANK_CODES = [((1, 49), "BNP Paribas Fortis"), ((50, 99), "Belfius"), ((103, 108), "Crelan"),
               ((140, 149), "BNP Paribas Fortis"), ((200, 214), "BNP Paribas Fortis"),
               ((220, 298), "BNP Paribas Fortis"), ((310, 399), "ING"), ((400, 499), "KBC"),
               ((730, 731), "KBC"), ((732, 732), "CBC"), ((733, 741), "KBC"), ((742, 742), "CBC"),
               ((743, 749), "KBC"), ((979, 980), "Argenta")]


def guess_bank(iban: str | None) -> str | None:
    if not iban or not iban.startswith("BE") or len(iban) != 16:
        return None
    code = int(iban[4:7])
    for (lo, hi), name in _BANK_CODES:
        if lo <= code <= hi:
            return name
    return None


def account_id_for(iban: str | None, fallback: str) -> str:
    return f"acc:{iban}" if iban else fallback


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-") or "account"


def store(conn: sqlite3.Connection, accounts: list[dict], txs: list[dict], balances: list[dict], source: str) -> dict:
    """Upsert accounts and balances, enrich + insert transactions, skipping rows already synced from another source."""
    for a in accounts:
        db.upsert_account(conn, **a)
    for b in balances:
        db.set_balance(conn, b["account_id"], b["as_of"], b["balance_type"], b["amount"], b.get("currency", "EUR"))
    ctx = Context.from_db(conn)

    # Cross-source de-duplication: if the same account is also synced via Enable Banking (or another file
    # format), skip rows matching an existing transaction on (account, date, amount), count-for-count.
    by_acc: dict[str, list[dict]] = {}
    for t in txs:
        by_acc.setdefault(t["account_id"], []).append(t)
    keep = []
    skipped = 0
    for acc_id, items in by_acc.items():
        existing = Counter(
            (r[0], round(r[1], 2)) for r in conn.execute(
                "SELECT booking_date, amount FROM transactions WHERE account_id=? AND source!=? AND status='booked'",
                (acc_id, source)))
        for t in items:
            k = (t["booking_date"], round(t["amount"], 2))
            if existing.get(k, 0) > 0:
                existing[k] -= 1
                skipped += 1
                continue
            keep.append(enrich(t, ctx))
    n_new = db.insert_transactions(conn, keep)
    conn.commit()
    if n_new:
        recategorize(conn)
    return {"accounts": [a["id"] for a in accounts], "rows_in_file": len(txs), "new_transactions": n_new,
            "already_present": len(keep) - n_new, "skipped_as_synced_elsewhere": skipped}


def parse_any(path: str | Path, account_name: str | None = None, bank: str | None = None,
              account_type: str = "current") -> tuple[list, list, list, str]:
    """(accounts, transactions, balances, source) for a CSV, CAMT.053 or CODA file."""
    path = Path(path)
    head = path.read_bytes()[:4096].lstrip(b"\xef\xbb\xbf").lstrip()
    from .coda import is_coda, parse_coda
    if is_coda(head) or path.suffix.lower() in (".cod", ".coda"):
        return (*parse_coda(path, bank=bank, account_type=account_type), "coda")
    if (head.startswith(b"<") and b"camt.05" in head) or path.suffix.lower() == ".xml":
        from .camt053 import parse_camt
        return (*parse_camt(path, bank=bank, account_type=account_type), "camt053")
    from .csv_import import parse_csv
    return (*parse_csv(path, account_name=account_name, bank=bank, account_type=account_type), "csv")


def import_history(conn: sqlite3.Connection, paths, account_name: str | None = None, bank: str | None = None,
                   only_before_linked: bool = True) -> dict:
    """Import bank files (CSV, CAMT.053, CODA; files or a folder) to fill in history the bank link doesn't give, e.g.
    KBC before August 2026. With only_before_linked (default), rows on or after the first day an account already
    has from the bank link are left out, so the linked data is never touched; the other rows are de-duplicated as
    usual."""
    files = []
    for p in ([paths] if isinstance(paths, (str, Path)) else paths):
        q = Path(p).expanduser()
        files += sorted(x for x in q.iterdir() if x.is_file() and x.suffix.lower() in
                        (".csv", ".txt", ".xml", ".cod", ".coda")) if q.is_dir() else [q]
    if not files:
        raise ValueError("No files found (CSV, CAMT.053 XML or CODA expected).")
    results = []
    for f in files:
        try:
            accounts, txs, balances, source = parse_any(f, account_name, bank)
        except Exception as e:  # noqa: BLE001 - report per file
            results.append({"file": f.name, "error": str(e)})
            continue
        cut, before_cut = {}, 0
        if only_before_linked:
            for a in accounts:
                r = conn.execute("SELECT MIN(booking_date) FROM transactions WHERE account_id=? AND source=?",
                                 (a["id"], "enablebanking")).fetchone()
                if r and r[0]:
                    cut[a["id"]] = r[0]
            keep = [t for t in txs if t["account_id"] not in cut or t["booking_date"] < cut[t["account_id"]]]
            before_cut = len(txs) - len(keep)
            txs = keep
            # a balance from the file is only kept when it predates the linked history (never overrides a synced one)
            balances = [b for b in balances if b["account_id"] not in cut or b["as_of"] < cut[b["account_id"]]]
            for a in accounts:
                if a["id"] in cut:  # the linked account keeps its own details
                    a.update({k: None for k in ("name", "bank", "account_type")})
                    a["source"] = conn.execute("SELECT source FROM accounts WHERE id=?", (a["id"],)).fetchone()[0]
        res = store(conn, accounts, txs, balances, source)
        res.update({"file": f.name, "format": source, "rows_from_linked_period_left_out": before_cut,
                    "linked_history_starts": {k[-4:]: v for k, v in cut.items()}})
        results.append(res)
    return {"files": results, "new_transactions": sum(r.get("new_transactions", 0) for r in results)}


def import_file(conn: sqlite3.Connection, path: str | Path, account_name: str | None = None,
                bank: str | None = None, account_type: str = "current") -> dict:
    path = Path(path)
    head = path.read_bytes()[:4096].lstrip(b"\xef\xbb\xbf").lstrip()
    from .coda import is_coda, parse_coda
    if is_coda(head) or path.suffix.lower() in (".cod", ".coda"):
        accounts, txs, balances = parse_coda(path, bank=bank, account_type=account_type)
        return store(conn, accounts, txs, balances, "coda")
    if (head.startswith(b"<") and b"camt.05" in head) or path.suffix.lower() == ".xml":
        from .camt053 import parse_camt
        accounts, txs, balances = parse_camt(path, bank=bank, account_type=account_type)
        return store(conn, accounts, txs, balances, "camt053")
    from .csv_import import parse_csv
    accounts, txs, balances = parse_csv(path, account_name=account_name, bank=bank, account_type=account_type)
    return store(conn, accounts, txs, balances, "csv")


__all__ = ["import_file", "import_history", "parse_any", "guess_bank", "account_id_for", "clean_iban", "slug", "store"]
