"""CODA (Belgian coded statement of account, Febelfin standard): fixed-width 128-character records.

Record types used:
  0  header (creation date)                       1  old balance (account, currency, holder)
  2.1 movement (amount, dates, transaction code, communication)
  2.2 movement continued (communication, counterparty BIC)
  2.3 movement continued (counterparty account and name, communication)
  3.x information records (more communication)    8  new balance               9  trailer
Movements whose transaction-code type is a detail of a globalised amount (5-9) are skipped: their total is booked
as one movement already.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

from .. import db
from ..money import clean_iban
from . import account_id_for, guess_bank


def _d(s: str) -> str | None:
    s = s.strip()
    if len(s) != 6 or not s.isdigit() or s == "000000":
        return None
    dd, mm, yy = int(s[:2]), int(s[2:4]), int(s[4:])
    try:
        return date(2000 + yy if yy < 80 else 1900 + yy, mm, dd).isoformat()
    except ValueError:
        return None


def _amount(sign: str, digits: str) -> float:
    v = int(digits) / 1000.0 if digits.strip().isdigit() else 0.0
    return -v if sign == "1" else v


def _account(structure: str, field: str) -> tuple[str | None, str]:
    """(IBAN or account number, currency) from the 37-character account field."""
    if structure in ("2", "3"):     # IBAN (Belgian / foreign): 34 + currency
        return clean_iban(field[:34].strip()) or field[:34].strip() or None, field[34:37].strip() or "EUR"
    if structure == "0":            # Belgian account number: 12 + space + currency
        return field[:12].strip() or None, field[13:16].strip() or "EUR"
    return field[:34].strip() or None, field[34:37].strip() or "EUR"


def is_coda(head: bytes) -> bool:
    lines = head.decode("latin-1", errors="replace").splitlines()
    return bool(lines) and lines[0].startswith("0000") and len(lines[0].rstrip("\r\n")) in (127, 128) and \
        any(l.startswith("1") for l in lines[1:4])


def _comm(raw: str, structured: str) -> tuple[str, str | None]:
    """(free text, structured reference +++xxx/xxxx/xxxxx+++ when given)."""
    if structured == "1" and raw[:3] == "101":
        n = raw[3:15]
        if n.isdigit():
            return "", f"+++{n[:3]}/{n[3:7]}/{n[7:]}+++"
    if structured == "1":
        return raw[3:].strip(), None
    return raw.strip(), None


def parse_coda(path: str | Path, bank: str | None = None, account_type: str = "current"):
    text = Path(path).read_bytes().decode("latin-1", errors="replace")
    accounts, txs, balances = {}, [], []
    acc_id = iban = currency = holder = None
    cur = None
    seq = 0

    def flush():
        nonlocal cur
        if cur and not cur.pop("_skip", False):
            desc = " ".join(x for x in cur.pop("_comm", []) if x).strip()
            cur["remittance"] = (cur.get("remittance") or "") + (" " if cur.get("remittance") and desc else "") + desc \
                or None
            txs.append(cur)
        cur = None

    for line in text.splitlines():
        if len(line) < 2:
            continue
        line = line.ljust(128)
        rt = line[0]
        if rt == "1":
            flush()
            iban, currency = _account(line[1], line[5:42])
            holder = line[64:90].strip() or None
            acc_id = account_id_for(iban if iban and iban[:2].isalpha() else None,
                                    f"coda:{iban or 'unknown'}")
            name = line[90:125].strip() or holder or (f"…{iban[-4:]}" if iban else "CODA")
            accounts.setdefault(acc_id, {"id": acc_id, "source": "coda", "bank": bank or guess_bank(iban), "iban": iban,
                                         "name": holder or name, "currency": currency, "account_type": account_type})
            d = _d(line[58:64])
            if d:
                balances.append({"account_id": acc_id, "as_of": d, "balance_type": "booked",
                                 "amount": _amount(line[42], line[43:58]), "currency": currency})
        elif rt == "2" and line[1] == "1":
            flush()
            code = line[53:61]
            seq += 1
            free, ref = _comm(line[62:115], line[61])
            cur = {"account_id": acc_id, "booking_date": _d(line[115:121]) or _d(line[47:53]),
                   "value_date": _d(line[47:53]), "amount": round(_amount(line[31], line[32:47]), 2),
                   "currency": currency or "EUR", "status": "booked", "structured_ref": ref,
                   "bank_tx_code": code.strip() or None, "source": "coda", "_comm": [free],
                   "_skip": code[:1] in "56789", "description": None,
                   "_ref": line[10:31].strip()}
        elif rt == "2" and line[1] == "2" and cur:
            cur["_comm"].append(line[10:63].strip())
        elif rt == "2" and line[1] == "3" and cur:
            cp_acc = line[10:47].strip()
            cur["counterparty_iban"] = clean_iban(cp_acc[:34].strip()) if cp_acc else None
            cur["counterparty_name"] = line[47:82].strip() or None
            cur["_comm"].append(line[82:125].strip())
        elif rt == "3" and cur:
            part = line[40:113] if line[1] == "1" else line[10:115] if line[1] == "2" else line[10:100]
            cur["_comm"].append(part.strip())
        elif rt == "8":
            flush()
            d = _d(line[57:63])
            if d and acc_id:
                balances.append({"account_id": acc_id, "as_of": d, "balance_type": "booked",
                                 "amount": _amount(line[41], line[42:57]), "currency": currency or "EUR"})
        elif rt == "9":
            flush()
    flush()
    out = []
    for i, t in enumerate(txs):
        ref = t.pop("_ref", "")
        if not t["booking_date"] or t["account_id"] is None:
            continue
        t["id"] = db.make_id("coda", t["account_id"], t["booking_date"], t["amount"], ref, t.get("remittance"), i)
        out.append(t)
    return list(accounts.values()), out, balances
