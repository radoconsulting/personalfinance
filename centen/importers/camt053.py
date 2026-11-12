"""ISO 20022 CAMT.053 (bank-to-customer statement) and CAMT.052 (account report) importer.

Most Belgian banks offer this XML export in their web banking (often labelled 'XML', 'ISO 20022' or 'CAMT').
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

from .. import db
from ..money import clean_iban, parse_date
from . import account_id_for, guess_bank, slug


def _strip_ns(root: ET.Element) -> ET.Element:
    for el in root.iter():
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
    return root


def _t(el: ET.Element | None, path: str) -> str | None:
    if el is None:
        return None
    found = el.find(path)
    if found is None or found.text is None:
        return None
    return found.text.strip() or None


def _first(el, *paths):
    for p in paths:
        v = _t(el, p)
        if v:
            return v
    return None


def _party_name(td, role: str):
    return _first(td, f"RltdPties/{role}/Nm", f"RltdPties/{role}/Pty/Nm", f"RltdPties/Ultmt{role}/Nm",
                  f"RltdPties/Ultmt{role}/Pty/Nm")


def _party_iban(td, role: str):
    return clean_iban(_first(td, f"RltdPties/{role}Acct/Id/IBAN", f"RltdPties/{role}Acct/Id/Othr/Id"))


def parse_camt(path: str | Path, bank: str | None = None, account_type: str = "current"):
    data = Path(path).read_bytes()
    if b"<!DOCTYPE" in data[:2048] or b"<!ENTITY" in data:
        raise ValueError("Refusing to parse XML with a DOCTYPE/ENTITY declaration.")
    root = _strip_ns(ET.fromstring(data))
    stmts = root.findall(".//Stmt") + root.findall(".//Rpt")
    accounts, txs, balances = {}, [], []
    seen = Counter()
    for st in stmts:
        iban = clean_iban(_first(st, "Acct/Id/IBAN", "Acct/Id/Othr/Id"))
        ccy = _t(st, "Acct/Ccy") or "EUR"
        acc_id = account_id_for(iban, f"camt:{slug(_t(st, 'Acct/Nm') or Path(path).stem)}")
        bank_name = bank or _first(st, "Acct/Svcr/FinInstnId/Nm") or guess_bank(iban)
        accounts[acc_id] = {"id": acc_id, "source": "camt053", "bank": bank_name, "iban": iban,
                            "name": _t(st, "Acct/Nm") or (f"{bank_name or 'Account'} …{iban[-4:]}" if iban else "Account"),
                            "currency": ccy, "account_type": account_type}

        for bal in st.findall("Bal"):
            code = _first(bal, "Tp/CdOrPrtry/Cd", "Tp/CdOrPrtry/Prtry")
            amt = _t(bal, "Amt")
            if not code or amt is None:
                continue
            sign = -1 if _t(bal, "CdtDbtInd") == "DBIT" else 1
            when = parse_date(_first(bal, "Dt/Dt", "Dt/DtTm"))
            kind = {"CLBD": "booked", "ITBD": "booked", "CLAV": "available", "ITAV": "available"}.get(code)
            if kind and when:
                balances.append({"account_id": acc_id, "as_of": when, "balance_type": kind,
                                 "amount": sign * float(amt), "currency": ccy})

        for n in st.findall("Ntry"):
            status = (_first(n, "Sts/Cd", "Sts") or "BOOK").upper()
            if status in ("INFO", "RJCT", "CNCL"):
                continue
            ind = _t(n, "CdtDbtInd")
            sign = -1 if ind == "DBIT" else 1
            bdate = parse_date(_first(n, "BookgDt/Dt", "BookgDt/DtTm", "ValDt/Dt", "ValDt/DtTm"))
            vdate = parse_date(_first(n, "ValDt/Dt", "ValDt/DtTm"))
            if not bdate:
                continue
            code = "-".join(x for x in (_t(n, "BkTxCd/Domn/Cd"), _t(n, "BkTxCd/Domn/Fmly/Cd"),
                                         _t(n, "BkTxCd/Domn/Fmly/SubFmlyCd")) if x) or _t(n, "BkTxCd/Prtry/Cd")
            addtl = _t(n, "AddtlNtryInf")
            ref = _first(n, "AcctSvcrRef", "NtryRef")
            details = n.findall("NtryDtls/TxDtls")
            entry_amt = float(_t(n, "Amt") or 0)
            parts = details if details else [None]
            for idx, td in enumerate(parts):
                amt = entry_amt
                if td is not None and len(parts) > 1:
                    a = _first(td, "AmtDtls/TxAmt/Amt", "Amt")
                    amt = float(a) if a else entry_amt / len(parts)
                role = "Cdtr" if sign < 0 else "Dbtr"
                ustrd = " ".join(x.text.strip() for x in (td.findall("RmtInf/Ustrd") if td is not None else []) if x.text)
                strd = _t(td, "RmtInf/Strd/CdtrRefInf/Ref")
                tx = {
                    "account_id": acc_id, "booking_date": bdate, "value_date": vdate,
                    "amount": round(sign * amt, 2), "currency": n.find("Amt").get("Ccy", ccy) if n.find("Amt") is not None else ccy,
                    "status": "pending" if status == "PDNG" else "booked",
                    "counterparty_name": _party_name(td, role) if td is not None else None,
                    "counterparty_iban": _party_iban(td, role) if td is not None else None,
                    "description": " ".join(x for x in (addtl, _t(td, "AddtlTxInf")) if x) or None,
                    "remittance": " ".join(x for x in (strd, ustrd) if x) or None,
                    "mandate_id": _t(td, "Refs/MndtId"),
                    "creditor_id": _first(td, "RltdPties/Cdtr/Id/PrvtId/Othr/Id", "RltdPties/Cdtr/Pty/Id/PrvtId/Othr/Id")
                    if td is not None and sign < 0 else None,
                    "bank_tx_code": code, "source": "camt053",
                }
                content = (acc_id, bdate, tx["amount"], tx["counterparty_iban"], tx["counterparty_name"], tx["remittance"])
                seen[content] += 1
                tx["id"] = db.make_id("camt", acc_id, ref, idx, bdate, tx["amount"]) if ref \
                    else db.make_id("camt", *content, seen[content])
                tx["raw_json"] = {"ref": ref, "code": code, "status": status}
                txs.append(tx)
    if not stmts:
        raise ValueError("No <Stmt> or <Rpt> found: is this a CAMT.053/052 file?")
    return list(accounts.values()), txs, balances
