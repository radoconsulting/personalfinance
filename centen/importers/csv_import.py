"""Generic importer for CSV exports of Belgian banks.

Rather than one parser per bank, it recognises column headers in Dutch, French and English (KBC, BNP Paribas
Fortis, Fintro, Hello bank!, Belfius, ING, Argenta, Crelan, AXA, Beobank, Keytrade, ...), finds the header row
even after a preamble, sniffs the delimiter and parses Belgian amounts (1.234,56) and dates (dd/mm/yyyy).
"""
from __future__ import annotations

import csv
import io
import re
from collections import Counter
from pathlib import Path

from .. import db
from ..money import clean_iban, normalize_text, parse_amount, parse_date
from . import account_id_for, guess_bank, slug

ROLE_ALIASES: dict[str, list[str]] = {
    "account": ["rekeningnummer", "rekening", "nr v/d rekening", "nummer van de rekening", "uw rekening",
                "numero de compte", "n de compte", "compte", "votre compte", "account", "account number", "iban",
                "rekeningnummer opdrachtgever"],
    "account_name": ["naam van de rekening", "rubrieknaam", "nom du compte", "account name"],
    "date": ["boekingsdatum", "boekdatum", "datum", "uitvoeringsdatum", "verrichtingsdatum", "datum verrichting",
             "date", "date de comptabilisation", "date d'execution", "date comptable", "date d'operation",
             "booking date", "transaction date", "execution date"],
    "value_date": ["valutadatum", "valuta datum", "valuta", "date valeur", "date de valeur", "value date"],
    "amount": ["bedrag", "montant", "amount", "bedrag (eur)", "montant (eur)", "bedrag in eur"],
    "credit": ["credit", "bij", "creditbedrag", "ontvangsten", "credit amount", "entrees"],
    "debit": ["debet", "debit", "af", "debetbedrag", "uitgaven", "debit amount", "sorties"],
    "currency": ["munt", "munteenheid", "devies", "valuta rekening", "devise", "devise du compte", "currency",
                 "munt rekening"],
    "cp_name": ["naam tegenpartij", "naam van de tegenpartij", "naam tegenpartij bevat", "nom de la contrepartie",
                "nom contrepartie", "contrepartie", "counterparty name", "naam begunstigde", "naam opdrachtgever",
                "tegenpartij naam", "beneficiaire"],
    "cp_iban": ["rekening tegenpartij", "rekeningnummer tegenpartij", "tegenpartij", "compte de la contrepartie",
                "compte contrepartie", "numero de compte de la contrepartie", "counterparty account", "counterparty",
                "tegenrekening", "iban tegenpartij"],
    "description": ["omschrijving", "beschrijving", "details", "detail van de omzet", "transactie", "type verrichting",
                    "description", "detail", "libelle", "type d'operation", "omschrijving verrichting", "type"],
    "message": ["mededeling", "mededelingen", "vrije mededeling", "bericht", "communication", "communications",
                "message", "communication libre"],
    "structured": ["gestructureerde mededeling", "communication structuree"],
    "balance": ["saldo", "solde", "balance", "saldo na verrichting", "nieuw saldo"],
    "ref": ["volgnummer", "referentie", "omzetnummer", "transactienummer", "reference", "numero de sequence",
            "id", "transaction id", "referentie van de bank"],
    "status": ["status", "statut"],
}
_ALIAS_TO_ROLE = {a: role for role, aliases in ROLE_ALIASES.items() for a in aliases}
_REFUSED = re.compile(r"geweigerd|refuse|rejected|geannuleerd|annule|cancel")


def _norm_header(h: str) -> str:
    h = normalize_text(h).strip(" :;\"'")
    return re.sub(r"\s+", " ", h)


def _decode(raw: bytes) -> str:
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _sniff_delimiter(text: str) -> str:
    sample = "\n".join(text.splitlines()[:40])
    counts = {d: sample.count(d) for d in (";", "\t", ",", "|")}
    return max(counts, key=counts.get) if any(counts.values()) else ";"


def _find_header(rows: list[list[str]]) -> int:
    best, best_score = -1, 0
    for i, row in enumerate(rows[:40]):
        score = sum(1 for c in row if _norm_header(c) in _ALIAS_TO_ROLE)
        if score > best_score:
            best, best_score = i, score
    if best_score < 2:
        raise ValueError("Could not find a header row with recognisable columns (date, amount, ...). "
                         "Export the CSV with column headers, or open an issue with an anonymised sample.")
    return best


def _looks(values: list[str], kind: str) -> float:
    vals = [v for v in values if v and v.strip()]
    if not vals:
        return 0.0
    if kind == "date":
        ok = sum(1 for v in vals if parse_date(v))
    elif kind == "iban":
        ok = sum(1 for v in vals if clean_iban(v) or re.fullmatch(r"\s*[A-Z]{2}\d{2}[\dA-Z ]{8,}\s*", v.upper()))
    elif kind == "currency":
        ok = sum(1 for v in vals if re.fullmatch(r"\s*[A-Z]{3}\s*", v.upper()))
    else:
        ok = 0
    return ok / len(vals)


def map_columns(header: list[str], data: list[list[str]]) -> dict[str, int]:
    roles: dict[str, int] = {}
    multi: dict[str, list[int]] = {}
    sample = data[:50]

    def col(i):
        return [r[i] if i < len(r) else "" for r in sample]

    for i, h in enumerate(header):
        nh = _norm_header(h)
        role = _ALIAS_TO_ROLE.get(nh)
        if role is None:
            # loose match: header starts with an alias ("Bedrag in EUR", "Naam tegenpartij bevat")
            for alias, r in sorted(_ALIAS_TO_ROLE.items(), key=lambda kv: -len(kv[0])):
                if len(alias) > 4 and nh.startswith(alias):
                    role = r
                    break
        if role is None:
            continue
        values = col(i)
        # Disambiguate headers that mean different things at different banks.
        if nh == "valuta":
            role = "value_date" if _looks(values, "date") > 0.6 else "currency"
        elif nh in ("tegenpartij", "contrepartie", "counterparty"):
            role = "cp_iban" if _looks(values, "iban") > 0.5 else "cp_name"
        elif nh == "rekening" and _looks(values, "iban") < 0.5:
            continue
        elif nh == "type" and "description" in roles:
            continue
        if role in ("date", "value_date") and _looks(values, "date") < 0.6:
            continue
        if role in ("description", "message"):
            multi.setdefault(role, []).append(i)  # e.g. ING: Omschrijving + Detail van de omzet
            roles.setdefault(role, i)
        elif role not in roles:
            roles[role] = i
    if "date" not in roles and "value_date" in roles:
        roles["date"] = roles["value_date"]
    if "date" not in roles:
        raise ValueError(f"No date column found in headers: {header}")
    if "amount" not in roles and not ("credit" in roles or "debit" in roles):
        raise ValueError(f"No amount column found in headers: {header}")
    roles["_multi"] = multi  # type: ignore[assignment]
    return roles


def parse_csv(path: str | Path, account_name: str | None = None, bank: str | None = None,
              account_type: str = "current"):
    text = _decode(Path(path).read_bytes())
    delim = _sniff_delimiter(text)
    rows = [r for r in csv.reader(io.StringIO(text), delimiter=delim)]
    rows = [r for r in rows if any(c.strip() for c in r)]
    h = _find_header(rows)
    header, data = rows[h], rows[h + 1:]
    roles = map_columns(header, data)

    multi = roles.pop("_multi", {})

    def get(r, role):
        if role in multi:
            parts = [r[i].strip() for i in multi[role] if i < len(r) and r[i] and r[i].strip()]
            return " | ".join(dict.fromkeys(parts))
        i = roles.get(role)
        return r[i].strip() if i is not None and i < len(r) and r[i] is not None else ""

    fallback_id = f"csv:{slug(account_name or Path(path).stem)}"
    accounts: dict[str, dict] = {}
    parsed = []
    for r in data:
        d = parse_date(get(r, "date"))
        if not d:
            continue
        if _REFUSED.search(normalize_text(get(r, "status"))):
            continue
        if "amount" in roles:
            amount = parse_amount(get(r, "amount"))
        else:
            cr, de = parse_amount(get(r, "credit")) or 0.0, parse_amount(get(r, "debit")) or 0.0
            amount = abs(cr) - abs(de)
        if amount is None:
            continue
        iban = clean_iban(get(r, "account"))
        acc_id = account_id_for(iban, fallback_id)
        if acc_id not in accounts:
            accounts[acc_id] = {
                "id": acc_id, "source": "csv", "bank": bank or guess_bank(iban), "iban": iban,
                "name": account_name or get(r, "account_name") or (f"{bank or guess_bank(iban) or 'Account'} "
                                                                    f"…{iban[-4:]}" if iban else Path(path).stem),
                "currency": (get(r, "currency") or "EUR").upper()[:3], "account_type": account_type,
            }
        msg = " ".join(x for x in (get(r, "structured"), get(r, "message")) if x)
        cp_raw = get(r, "cp_iban")
        parsed.append({
            "account_id": acc_id, "booking_date": d, "value_date": parse_date(get(r, "value_date")),
            "amount": round(amount, 2), "currency": (get(r, "currency") or "EUR").upper()[:3], "status": "booked",
            "counterparty_name": get(r, "cp_name") or None, "counterparty_iban": clean_iban(cp_raw),
            "description": get(r, "description") or None, "remittance": msg or None,
            "ref": get(r, "ref"), "balance": parse_amount(get(r, "balance")) if "balance" in roles else None,
            "source": "csv", "raw_json": dict(zip(header, r)),
        })

    # Stable ids: bank reference when present, otherwise content hash + occurrence index (so re-importing an
    # overlapping export never duplicates rows, but two genuinely identical payments on one day both survive).
    seen = Counter()
    txs = []
    for t in parsed:
        content = (t["account_id"], t["booking_date"], t["amount"], t["counterparty_iban"], t["counterparty_name"],
                   t["description"], t["remittance"])
        seen[content] += 1
        ref = t.pop("ref")
        t["id"] = db.make_id("csv", t["account_id"], ref, t["booking_date"], t["amount"]) if ref and len(ref) >= 4 \
            else db.make_id("csv", *content, seen[content])
        txs.append(t)

    # Latest balance per account, if the export has a balance column.
    balances = []
    for acc_id in accounts:
        rows_acc = [t for t in txs if t["account_id"] == acc_id and t.get("balance") is not None]
        if rows_acc:
            newest_first = rows_acc[0]["booking_date"] >= rows_acc[-1]["booking_date"]
            last_date = max(t["booking_date"] for t in rows_acc)
            cands = [t for t in rows_acc if t["booking_date"] == last_date]
            pick = cands[0] if newest_first else cands[-1]
            balances.append({"account_id": acc_id, "as_of": last_date, "balance_type": "booked",
                             "amount": pick["balance"], "currency": accounts[acc_id]["currency"]})
    for t in txs:
        t.pop("balance", None)
    return list(accounts.values()), txs, balances
