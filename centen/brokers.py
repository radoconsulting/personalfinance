"""Read broker exports into one transaction format, for portfolio management.

Supported (detected from the header, any language DEGIRO offers in NL / EN / FR / DE):
- DEGIRO Transactions.csv   (Transacties: every buy and sell, with EUR value, exchange rate and costs)
- DEGIRO Account.csv        (Rekeningoverzicht: dividends, dividend tax, interest, fees, stock-exchange tax,
                             deposits and withdrawals; trade rows are skipped because Transactions.csv has them)
- DEGIRO Portfolio.csv      (Portefeuille: holdings on the export date)
- LYNX / Interactive Brokers activity statement CSV (Trades, Dividends, Withholding Tax, Deposits & Withdrawals,
  Fees, Interest, Transaction Fees, Open Positions) and Flex Query CSVs (trades and cash transactions)
- Bitvavo transaction history CSV (buy, sell, deposit, withdrawal, staking and other rewards)

Every transaction is a dict with: date, time, kind, name, isin, symbol, quantity (units, positive), price, currency,
amount (cash effect in `currency`, signed: + = money into the broker account), amount_eur (same in EUR), fee_eur
(costs in EUR, positive), fx, order_id, description, ext (stable text used for the id).

kind: buy | sell | dividend | dividend_tax | interest | fee | tax | deposit | withdrawal | transfer_in |
      transfer_out | income | other
"""
from __future__ import annotations

import csv
import hashlib
import io
import re
from datetime import date, datetime

from .money import normalize_text, parse_amount
from .positions import CCY, ISIN, parse_text as parse_positions, read_text

KINDS = ("buy", "sell", "dividend", "dividend_tax", "interest", "fee", "tax", "deposit", "withdrawal", "transfer_in",
         "transfer_out", "income", "other")
BROKER_OF = {"degiro_transactions": "DEGIRO", "degiro_account": "DEGIRO", "degiro": "DEGIRO", "ibkr": "LYNX",
             "ibkr_flex": "LYNX", "bitvavo": "Bitvavo", "table": None}


def _h(s) -> str:
    return re.sub(r"\s+", " ", normalize_text(s or "")).strip()


def _num(s) -> float | None:
    if s is None:
        return None
    s = str(s).strip().replace(" ", "").replace("’", "").replace("'", "")
    if not s or s in ("-", "—", "--"):
        return None
    if re.fullmatch(r"-?\d+(\.\d+)?[eE]-?\d+", s):
        return float(s)
    return parse_amount(s)


def _date(s: str) -> str | None:
    s = (s or "").strip().strip('"')
    if not s:
        return None
    s = s.split(";")[0].split("T")[0] if re.match(r"\d{4}-\d{2}-\d{2}[T;]", s) else s
    for fmt in ("%d-%m-%Y", "%Y-%m-%d", "%d/%m/%Y", "%Y%m%d", "%d.%m.%Y", "%Y/%m/%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(s.split(" ")[0].split(",")[0], fmt).date().isoformat()
        except ValueError:
            continue
    for fmt in ("%a %b %d %Y", "%B %d, %Y", "%d %b %Y"):
        try:
            return datetime.strptime(" ".join(s.split(" ")[:4]) if fmt == "%a %b %d %Y" else s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _time(s: str) -> str | None:
    m = re.search(r"(\d{1,2}):(\d{2})(?::(\d{2}))?", s or "")
    if m:
        return f"{int(m.group(1)):02d}:{m.group(2)}" + (f":{m.group(3)}" if m.group(3) else "")
    m = re.search(r"[;T ](\d{2})(\d{2})(\d{2})$", s or "")
    return f"{m.group(1)}:{m.group(2)}:{m.group(3)}" if m else None


def _units(s) -> float | None:
    """A unit count in a DEGIRO Dutch export: '25.000' is twenty-five thousand (DEGIRO has no fractional units)."""
    s = (s or "").strip()
    if re.fullmatch(r"-?\d{1,3}(\.\d{3})+", s):
        return float(s.replace(".", ""))
    return _num(s)


def make_id(account_id: str, source: str, ext: str) -> str:
    return hashlib.sha256(f"{account_id}\x1f{source}\x1f{ext}".encode()).hexdigest()[:24]


def _rows(text: str) -> list[list[str]]:
    first = next((l for l in text.splitlines() if l.strip()), "")
    delim = max([";", ",", "\t"], key=first.count)
    return [r for r in csv.reader(io.StringIO(text), delimiter=delim)]


def _tx(**kw) -> dict:
    base = {k: None for k in ("date", "time", "kind", "name", "isin", "symbol", "quantity", "price", "currency",
                              "amount", "amount_eur", "fee_eur", "fx", "order_id", "description", "ext")}
    base.update(kw)
    for k in ("amount", "amount_eur", "fee_eur", "price", "quantity", "fx"):
        if base[k] is not None:
            base[k] = round(float(base[k]), 8 if k in ("quantity", "price", "fx") else 2)
    return base


# --- detection --------------------------------------------------------------------------------

def detect(text: str) -> str:
    head = text.lstrip("﻿")[:4000]
    if re.search(r"^(Statement|Trades|Open Positions|Dividends|Account Information),Header,", head, re.M):
        return "ibkr"
    rows = _rows(text)[:10]
    for r in rows:
        h = {_h(x) for x in r}
        joined = " | ".join(h)
        if ({"tradedate", "datetime", "date/time", "settledate", "reportdate"} & h) and \
                ({"symbol", "assetclass", "fxratetobase", "ibcommission", "currencyprimary"} & h):
            return "ibkr_flex"
        if "type" in h and ("currency" in h or "valuta" in h) and \
                re.search(r"quote price|price \(eur\)|received / paid|eur received|ontvangen / betaald", joined):
            return "bitvavo"
        if ({"valutadatum", "value date", "date de valeur", "valutadatum", "valuta"} & h or "valuta" in joined) and \
                ({"omschrijving", "description", "beschreibung"} & h) and ({"mutatie", "change", "mouvements", "anderung",
                                                                             "mutation"} & h or "saldo" in h):
            return "degiro_account"
        if "isin" in h and ({"aantal", "quantity", "quantite", "anzahl"} & h) and \
                ({"datum", "date", "datum"} & h) and ({"tijd", "time", "heure", "uhrzeit"} & h):
            return "degiro_transactions"
    return "positions"


# --- DEGIRO -----------------------------------------------------------------------------------

def _pair(r: list[str], i: int | None, header: list[str]) -> tuple[float | None, str | None]:
    """DEGIRO splits money into two columns (amount + currency, in either order) under one header."""
    if i is None or i >= len(r):
        return None, None
    a = r[i].strip()
    b = r[i + 1].strip() if i + 1 < len(r) and (i + 1 >= len(header) or not header[i + 1].strip()) else ""
    if CCY.match(a.upper()) and b:
        return _num(b), a.upper()
    if b and CCY.match(b.upper()):
        return _num(a), b.upper()
    return _num(a), None


def _find(header: list[str], *names, contains: tuple = (), exclude: tuple = ()) -> int | None:
    hs = [_h(x) for x in header]
    for i, x in enumerate(hs):
        if x in names and not any(e in x for e in exclude):
            return i
    for i, x in enumerate(hs):
        if x and any(c in x for c in contains) and not any(e in x for e in exclude):
            return i
    return None


def _header(rows, test) -> int:
    for i, r in enumerate(rows[:10]):
        if test({_h(x) for x in r}):
            return i
    raise ValueError("Header not found.")


def degiro_transactions(text: str) -> dict:
    rows = _rows(text)
    hi = _header(rows, lambda h: "isin" in h)
    H = rows[hi]
    c = {
        "date": _find(H, "datum", "date"), "time": _find(H, "tijd", "time", "heure", "uhrzeit"),
        "product": _find(H, "product", "produit", "produkt"), "isin": _find(H, "isin"),
        "qty": _find(H, "aantal", "quantity", "quantite", "anzahl"),
        "price": _find(H, "koers", "price", "cours", "kurs"),
        "local": _find(H, contains=("lokale waarde", "local value", "valeur locale", "kurswert", "lokaler")),
        "value": _find(H, "waarde", "value", "valeur", "wert", "waarde eur", "value eur", "valeur eur", "wert eur",
                       "value in eur", contains=("waarde eur", "value eur", "value in eur", "valeur eur"),
                       exclude=("lokale", "local", "locale", "lokaler")),
        "fx": _find(H, "wisselkoers", "exchange rate", "taux de change", "wechselkurs"),
        "autofx": _find(H, contains=("autofx",)),
        "costs": _find(H, contains=("transactiekosten", "transaction and", "transaction costs", "frais de transaction",
                                    "transaktionskosten", "kosten", "frais", "fees"), exclude=("autofx",)),
        "total": _find(H, "totaal", "total", "totaal eur", "total eur", "gesamt", "gesamt eur"),
        "order": _find(H, "order id", "order-id", "id de l'ordre", "orderid"),
    }
    out, warnings = [], []
    seen: dict[str, int] = {}
    for r in rows[hi + 1:]:
        if not any(x.strip() for x in r) or c["date"] is None or c["date"] >= len(r):
            continue
        d = _date(r[c["date"]])
        qty = _units(r[c["qty"]]) if c["qty"] is not None and c["qty"] < len(r) else None
        if not d or not qty:
            continue
        price, ccy = _pair(r, c["price"], H)
        local, lccy = _pair(r, c["local"], H)
        value, _ = _pair(r, c["value"], H)
        costs, _ = _pair(r, c["costs"], H)
        autofx, _ = _pair(r, c["autofx"], H)
        total, _ = _pair(r, c["total"], H)
        fx = _num(r[c["fx"]]) if c["fx"] is not None and c["fx"] < len(r) else None
        ccy = ccy or lccy or "EUR"
        if value is None and local is not None:
            value = local if ccy == "EUR" else (local / fx if fx else None)
        fee = -((costs or 0) + (autofx or 0))
        if value is None and total is not None:
            value = total + fee
        if value is None:
            warnings.append(f"{d}: no EUR value for {r[c['product']] if c['product'] is not None else '?'}")
        if local is None and price is not None:
            local = -qty * price
        name = r[c["product"]].strip() if c["product"] is not None else None
        isin = r[c["isin"]].strip().upper() if c["isin"] is not None else None
        order = r[c["order"]].strip() if c["order"] is not None and c["order"] < len(r) else None
        t = r[c["time"]].strip() if c["time"] is not None and c["time"] < len(r) else None
        ext = f"{order}|{d}|{t}|{isin}|{qty}|{price}"
        seen[ext] = seen.get(ext, 0) + 1
        out.append(_tx(date=d, time=_time(t), kind="buy" if qty > 0 else "sell", name=name, isin=isin or None,
                       quantity=abs(qty), price=price, currency=ccy, amount=local, amount_eur=value, fee_eur=fee or 0.0,
                       fx=fx, order_id=order or None, description=None, ext=f"{ext}|{seen[ext]}"))
    return {"format": "degiro_transactions", "transactions": out, "positions": None, "warnings": warnings}


_DG_SKIP = re.compile(r"valuta ?(creditering|debitering)|fx (credit|debit)|change de devise|devisen|cash sweep|"
                      r"geldrekening|cash account|compte (especes|espece)|geldkonto|geldmarktfonds|money market|"
                      r"fonds monetaire|geldmarkt|cash fund|reservation|reservering|koop \d|verkoop \d|^buy \d|"
                      r"^sell \d|achat \d|vente \d|kauf \d|verkauf \d|fund conversion|omzetting|conversion")
_DG_KINDS = [
    ("dividend_tax", r"dividendbelasting|dividend tax|impot sur dividende|impots sur dividende|retenue|"
                     r"dividendensteuer|quellensteuer|withholding|bronheffing|roerende voorheffing"),
    ("dividend", r"dividend|dividende|kapitaalsuitkering|capital return|return of capital|remboursement de capital"),
    ("tax", r"beurstaks|taxe sur les operations de bourse|stock exchange tax|financial transaction tax|\btob\b|"
            r"transaction tax|borsensteuer|taks op beursverrichtingen|beursbelasting|transactiebelasting|"
            r"taxe sur les transactions|belgian transaction"),
    ("skip", r"transactiekosten|kosten van derden|transaction costs|transaction and/or|frais de transaction|"
             r"transaktionskosten"),  # trade costs: Transactions.csv has them per trade
    ("income", r"securities lending|effectenlening|pret de titres|inkomsten uit|wertpapierleihe"),
    ("withdrawal", r"terugstorting|withdrawal|retrait|auszahlung|opname|uitbetaling"),
    ("deposit", r"storting|deposit|versement|einzahlung|ideal|sofort|bancontact"),
    ("interest", r"rente|interest|interet|zinsen"),
    ("fee", r"kosten|fee|frais|gebuhr|aansluiting|connection|servicekosten|adr|courtesy|promotie|vergoeding|"
            r"welkomst|welcome|bienvenue"),
]
# Corporate actions DEGIRO lists only in the account overview (not in Transactions.csv): units in or out without a
# normal trade. ISIN changes, product changes and capital increases are in Transactions.csv and are skipped here.
_DG_CORP = re.compile(r"^(stock dividend|dividende en actions|aandelendividend|verlopen obligatie|expired bond|"
                      r"obligation echue|afwikkeling hefboomproduct|settlement leveraged|expiration|expiratie|"
                      r"vervallen|waardeloos)[^:]*:\s*(koop|verkoop|buy|sell|achat|vente)\s+([\d.,]+)\s*@\s*([\d.,]+)")
_DG_BOND_CASH = re.compile(r"contante verrekening obligatie|aflossing obligatie|bond redemption|remboursement")


def degiro_account(text: str) -> dict:
    rows = _rows(text)
    hi = _header(rows, lambda h: bool({"omschrijving", "description", "beschreibung"} & h))
    H = rows[hi]
    c = {
        "date": _find(H, "datum", "date"), "time": _find(H, "tijd", "time", "heure", "uhrzeit"),
        "product": _find(H, "product", "produit", "produkt"), "isin": _find(H, "isin"),
        "desc": _find(H, "omschrijving", "description", "beschreibung"), "fx": _find(H, "fx", "t."),
        "mut": _find(H, "mutatie", "change", "mouvements", "anderung", "mutation", "betrag"),
        "saldo": _find(H, "saldo", "balance", "solde"),
        "order": _find(H, "order id", "order-id", "orderid", "id de l'ordre"),
    }
    parsed = []
    for r in rows[hi + 1:]:
        if not any(x.strip() for x in r) or c["date"] is None or c["date"] >= len(r):
            continue
        d = _date(r[c["date"]])
        if not d:
            continue
        amt, ccy = _pair(r, c["mut"], H)
        bal, _ = _pair(r, c["saldo"], H)
        g = (lambda k: r[c[k]].strip() if c[k] is not None and c[k] < len(r) else "")  # noqa: E731
        parsed.append({"date": d, "time": g("time"), "product": g("product"), "isin": g("isin").upper(),
                       "desc": g("desc"), "fx": _num(g("fx")), "amount": amt, "ccy": ccy, "balance": bal,
                       "order": g("order")})
    # Exchange rates DEGIRO used (foreign currency per EUR), from its currency-conversion rows. Dividends are usually
    # converted a day later, so the nearest conversion in either direction is used.
    rates: dict[str, list[tuple[date, float]]] = {}
    for p in parsed:
        if p["fx"] and p["ccy"] and p["ccy"] != "EUR":
            rates.setdefault(p["ccy"], []).append((date.fromisoformat(p["date"]), p["fx"]))

    def rate_for(ccy: str, d: str) -> float | None:
        cands = rates.get(ccy) or []
        dd = date.fromisoformat(d)
        return min(cands, key=lambda x: (abs((x[0] - dd).days), x[0] < dd))[1] if cands else None
    out, warnings, seen = [], [], {}
    # An expired bond pays out through a separate cash row for the same ISIN: pair them up front.
    paid_out: dict[int, float] = {}
    used_cash: set[int] = set()
    for p in parsed:
        m = _DG_CORP.match(_h(p["desc"]))
        if m and m.group(2) not in ("koop", "buy", "achat"):
            for b in parsed:
                if id(b) not in used_cash and _DG_BOND_CASH.search(_h(b["desc"])) and b["amount"] and \
                        b["isin"] == p["isin"] and (b["ccy"] or "EUR") == "EUR" and \
                        abs((date.fromisoformat(b["date"]) - date.fromisoformat(p["date"])).days) <= 10:
                    used_cash.add(id(b))
                    paid_out[id(p)] = b["amount"]
                    break
    for p in sorted(parsed, key=lambda x: (x["date"], x["time"])):
        desc = _h(p["desc"])
        corp = _DG_CORP.match(desc)
        if corp:
            qty = _units(corp.group(3))
            buy = corp.group(2) in ("koop", "buy", "achat")
            cash = paid_out.get(id(p), 0.0)
            ext = f"corp|{p['date']}|{p['time']}|{p['isin']}|{p['desc']}"
            out.append(_tx(date=p["date"], time=_time(p["time"]), kind="transfer_in" if buy else "sell",
                           name=p["product"] or None, isin=p["isin"] if ISIN.match(p["isin"] or "") else None,
                           quantity=qty, price=0.0 if buy else None, currency=p["ccy"] or "EUR",
                           amount=cash, amount_eur=cash, fee_eur=0.0, description=p["desc"][:120], ext=ext))
            continue
        if id(p) in used_cash:
            continue
        if p["amount"] is None or not desc or _DG_SKIP.search(desc):
            continue
        kind = next((k for k, rx in _DG_KINDS if re.search(rx, desc)), "other")
        if kind == "skip":
            continue
        if p["order"] and kind not in ("tax", "dividend", "dividend_tax"):
            continue  # trades and their costs come from Transactions.csv; stock-exchange tax only appears here
        if kind == "interest" and p["amount"] < 0:
            kind = "fee"  # debit interest / negative interest is a cost
        if kind in ("deposit", "withdrawal"):
            kind = "deposit" if p["amount"] > 0 else "withdrawal"
        ccy = p["ccy"] or "EUR"
        if ccy == "EUR":
            eur = p["amount"]
        else:
            rate = p["fx"] or rate_for(ccy, p["date"])
            eur = p["amount"] / rate if rate else None
            if eur is None:
                warnings.append(f"{p['date']}: no exchange rate for {ccy} ({p['desc'][:40]})")
        ext = f"{p['date']}|{p['time']}|{p['isin']}|{p['desc']}|{p['amount']}|{p['balance']}"
        seen[ext] = seen.get(ext, 0) + 1
        out.append(_tx(date=p["date"], time=_time(p["time"]), kind=kind, name=p["product"] or None,
                       isin=p["isin"] if ISIN.match(p["isin"] or "") else None, currency=ccy, amount=p["amount"],
                       amount_eur=eur, fee_eur=0.0, fx=None if ccy == "EUR" else (p["amount"] / eur if eur else None),
                       description=p["desc"][:120], ext=f"{ext}|{seen[ext]}"))
    return {"format": "degiro_account", "transactions": out, "positions": None, "warnings": warnings}


# --- Interactive Brokers / LYNX ----------------------------------------------------------------

def _sections(text: str):
    sections: dict[str, list[dict]] = {}
    headers: dict[str, list[str]] = {}
    for r in csv.reader(io.StringIO(text.lstrip("﻿"))):
        if len(r) < 3:
            continue
        sec, kind = r[0].strip(), r[1].strip()
        if kind == "Header":
            headers[sec] = [x.strip() for x in r[2:]]
        elif kind in ("Data", "Total", "SubTotal") and sec in headers:
            sections.setdefault(sec, []).append({"_kind": kind, **dict(zip(headers[sec], r[2:]))})
    return sections


_IB_INSTR = re.compile(r"^\s*([A-Z0-9.\- ]+?)\s*\(([A-Z]{2}[A-Z0-9]{9}\d)\)")


def ibkr(text: str) -> dict:
    S = _sections(text)
    base = "EUR"
    for row in S.get("Account Information", []):
        if row.get("Field Name") == "Base Currency":
            base = (row.get("Field Value") or "EUR").strip().upper()
    fx = {base: 1.0}
    for row in S.get("Base Currency Exchange Rate", []):
        rate = _num(row.get("Rate"))
        if row.get("Currency") and rate:
            fx[row["Currency"].strip().upper()] = rate
    info = {}
    for row in S.get("Financial Instrument Information", []):
        if row.get("Symbol"):
            info[row["Symbol"].strip()] = {"name": (row.get("Description") or "").strip() or None,
                                           "isin": (row.get("Security ID") or "").strip() or None}
    warnings, out, approx = [], [], set()

    def eur(amount, ccy):
        if amount is None:
            return None
        if ccy == base == "EUR":
            return amount
        if base != "EUR" or ccy not in fx:
            warnings.append(f"no EUR rate for {ccy}")
            return None
        approx.add(ccy)
        return amount * fx[ccy]

    for i, row in enumerate(S.get("Trades", [])):
        if row["_kind"] != "Data" or (row.get("DataDiscriminator") or "Order") not in ("Order", "Trade", "Execution"):
            continue
        cat = _h(row.get("Asset Category"))
        if "forex" in cat or "cash" == cat:
            continue
        sym = (row.get("Symbol") or "").strip()
        qty = _num(row.get("Quantity"))
        if not qty:
            continue
        ccy = (row.get("Currency") or base).strip().upper()
        proceeds = _num(row.get("Proceeds"))
        comm = _num(row.get("Comm/Fee") or row.get("Comm in EUR") or row.get("Commission"))
        dt = row.get("Date/Time") or row.get("TradeDate") or ""
        meta = info.get(sym, {})
        isin = meta.get("isin") if meta.get("isin") and ISIN.match(meta["isin"]) else None
        out.append(_tx(date=_date(dt), time=_time(dt), kind="buy" if qty > 0 else "sell", name=meta.get("name") or sym,
                       isin=isin, symbol=sym, quantity=abs(qty), price=_num(row.get("T. Price")), currency=ccy,
                       amount=proceeds, amount_eur=eur(proceeds, ccy), fee_eur=-(eur(comm, ccy) or 0.0),
                       order_id=None, description=cat or None,
                       ext=f"trade|{dt}|{sym}|{qty}|{row.get('T. Price')}|{proceeds}|{i}"))

    def cash(section, kind_for):
        for i, row in enumerate(S.get(section, [])):
            ccy = (row.get("Currency") or "").strip().upper()
            if row["_kind"] != "Data" or not ccy or ccy.startswith("TOTAL"):
                continue
            amt = _num(row.get("Amount"))
            d = _date(row.get("Date") or row.get("Settle Date") or row.get("Date/Time") or "")
            if amt is None or not d:
                continue
            desc = (row.get("Description") or "").strip()
            m = _IB_INSTR.match(desc)
            sym, isin = (m.group(1).strip(), m.group(2)) if m else ((row.get("Symbol") or "").strip() or None, None)
            kind = kind_for(amt, _h(desc))
            out.append(_tx(date=d, kind=kind, name=(info.get(sym or "", {}).get("name") or sym), isin=isin,
                           symbol=sym, currency=ccy, amount=amt, amount_eur=eur(amt, ccy), fee_eur=0.0,
                           description=desc[:120], ext=f"{section}|{d}|{desc}|{amt}|{i}"))

    cash("Dividends", lambda a, d: "dividend")
    cash("Payment In Lieu Of Dividends", lambda a, d: "dividend")
    cash("Withholding Tax", lambda a, d: "dividend_tax")
    cash("Deposits & Withdrawals", lambda a, d: "deposit" if a > 0 else "withdrawal")
    cash("Interest", lambda a, d: "interest" if a > 0 else "fee")
    cash("Fees", lambda a, d: "fee")
    cash("Other Fees", lambda a, d: "fee")
    cash("Transaction Fees", lambda a, d: "tax" if re.search(r"tax|taks|taxe|tob|stamp", d) else "fee")
    cash("Sales Tax Details", lambda a, d: "tax")
    positions = None
    if "Open Positions" in S:
        from .positions import _ibkr as ib_positions
        try:
            positions = ib_positions(text)
        except ValueError:
            positions = None
    if approx:
        warnings.append(f"amounts in {', '.join(sorted(approx))} converted at the statement's closing rate "
                        "(approximate for older transactions)")
    return {"format": "ibkr", "transactions": out, "positions": positions, "warnings": sorted(set(warnings))}


def ibkr_flex(text: str) -> dict:
    rows = _rows(text)
    hi = _header(rows, lambda h: bool({"symbol", "assetclass", "currencyprimary", "type"} & h))
    H = [x.strip() for x in rows[hi]]
    idx = {_h(x): i for i, x in enumerate(H)}

    def g(r, *names):
        for n in names:
            i = idx.get(n)
            if i is not None and i < len(r) and r[i].strip():
                return r[i].strip()
        return None
    out, warnings = [], []
    for k, r in enumerate(rows[hi + 1:]):
        if not any(x.strip() for x in r) or (len(r) > 0 and r[0] == H[0]):
            continue
        ccy = (g(r, "currencyprimary", "currency") or "EUR").upper()
        rate = _num(g(r, "fxratetobase")) or (1.0 if ccy == "EUR" else None)
        dt = g(r, "datetime", "tradedate", "date/time", "settledate", "reportdate") or ""
        d = _date(dt)
        if not d:
            continue
        sym = g(r, "symbol")
        isin = g(r, "isin", "securityid")
        isin = isin if isin and ISIN.match(isin) else None
        typ = _h(g(r, "type") or "")
        qty = _num(g(r, "quantity"))
        if qty and g(r, "tradeprice") is not None:
            if "cash" == _h(g(r, "assetclass") or "") or _h(g(r, "assetclass") or "") == "forex":
                continue
            proceeds = _num(g(r, "proceeds"))
            comm = _num(g(r, "ibcommission", "commission")) or 0.0
            out.append(_tx(date=d, time=_time(dt), kind="buy" if qty > 0 else "sell",
                           name=g(r, "description") or sym, isin=isin, symbol=sym, quantity=abs(qty),
                           price=_num(g(r, "tradeprice")), currency=ccy, amount=proceeds,
                           amount_eur=proceeds * rate if proceeds is not None and rate else None,
                           fee_eur=-(comm * rate) if rate else 0.0, fx=(1 / rate) if rate else None,
                           order_id=g(r, "iborderid", "orderid"), description=g(r, "assetclass"),
                           ext=f"flex|{g(r, 'tradeid', 'transactionid') or ''}|{dt}|{sym}|{qty}|{proceeds}|{k}"))
            continue
        amt = _num(g(r, "amount"))
        if amt is None or not typ:
            continue
        kind = ("dividend" if "dividend" in typ and "withholding" not in typ else
                "dividend_tax" if "withholding" in typ else
                ("deposit" if amt > 0 else "withdrawal") if "deposit" in typ or "withdraw" in typ else
                ("interest" if amt > 0 else "fee") if "interest" in typ else
                "tax" if "tax" in typ else "fee" if "fee" in typ or "commission" in typ else "other")
        out.append(_tx(date=d, kind=kind, name=sym, isin=isin, symbol=sym, currency=ccy, amount=amt,
                       amount_eur=amt * rate if rate else None, fee_eur=0.0, description=(g(r, "description") or "")[:120],
                       ext=f"flexcash|{g(r, 'transactionid') or ''}|{dt}|{typ}|{amt}|{k}"))
        if rate is None:
            warnings.append(f"no FXRateToBase for {ccy}")
    return {"format": "ibkr_flex", "transactions": out, "positions": None, "warnings": sorted(set(warnings))}


# --- Bitvavo ----------------------------------------------------------------------------------

_BV_BAD = re.compile(r"cancel|fail|reject|pending|awaiting|expired|annul")
_BV_INCOME = re.compile(r"staking|reward|rebate|affiliate|bonus|earn|airdrop|lending|interest|cashback|referral|"
                        r"promotion|distribution|campaign|manually|assigned|compensation")


def bitvavo(text: str) -> dict:
    rows = _rows(text)
    hi = _header(rows, lambda h: "type" in h)
    H = rows[hi]
    idx = {_h(x): i for i, x in enumerate(H)}

    def g(r, *names):
        for n in names:
            i = idx.get(n)
            if i is not None and i < len(r) and r[i].strip():
                return r[i].strip()
        return None
    out, warnings = [], []
    for k, r in enumerate(rows[hi + 1:]):
        if not any(x.strip() for x in r):
            continue
        typ = _h(g(r, "type"))
        status = _h(g(r, "status") or "")
        if not typ or _BV_BAD.search(status):
            continue
        when = " ".join(x for x in (g(r, "date"), g(r, "time")) if x) or g(r, "time", "timestamp") or ""
        d = _date(when)
        if not d:
            continue
        coin = (g(r, "currency", "valuta") or "").upper()
        qty = _num(g(r, "amount", "bedrag", "hoeveelheid"))
        qty = abs(qty) if qty is not None else None
        quote_ccy = (g(r, "quote currency") or "EUR").upper()
        price = _num(g(r, "quote price", "price (eur)", "prijs (eur)", "price"))
        paid_ccy = (g(r, "received / paid currency", "ontvangen / betaald valuta") or quote_ccy).upper()
        paid = _num(g(r, "received / paid amount", "eur received / paid", "eur ontvangen / betaald",
                      "ontvangen / betaald bedrag"))
        paid = abs(paid) if paid is not None else None
        fee_ccy = (g(r, "fee currency", "kosten valuta") or "EUR").upper()
        fee = abs(_num(g(r, "fee amount", "kosten bedrag", "fee")) or 0.0)
        txid = g(r, "transaction id", "txid", "id") or ""
        eur_value = paid if paid is not None and paid_ccy == "EUR" else \
            (qty * price if qty is not None and price is not None and quote_ccy == "EUR" else None)
        fee_eur = fee if fee_ccy == "EUR" else (fee * price if price and fee_ccy == coin and quote_ccy == "EUR" else 0.0)
        ext = f"{txid}|{when}|{typ}|{coin}|{qty}|{paid}|{k if not txid else ''}"
        base = dict(date=d, time=_time(when), name=coin, symbol=coin, description=typ, ext=ext, order_id=txid or None)
        if typ in ("buy", "sell", "koop", "verkoop", "aankoop"):
            if eur_value is None:
                warnings.append(f"{d}: {typ} {coin} not priced in EUR ({quote_ccy})")
            sign = -1 if typ in ("buy", "koop", "aankoop") else 1
            out.append(_tx(kind="buy" if sign < 0 else "sell", quantity=qty, price=price, currency="EUR",
                           amount=sign * (eur_value or 0), amount_eur=sign * eur_value if eur_value is not None else None,
                           fee_eur=fee_eur, **base))
        elif typ in ("deposit", "storting", "withdrawal", "opname", "opnemen"):
            dep = typ in ("deposit", "storting")
            if coin == "EUR":
                out.append(_tx(kind="deposit" if dep else "withdrawal", currency="EUR",
                               amount=qty if dep else -qty, amount_eur=qty if dep else -qty, fee_eur=fee_eur, **base))
            else:
                out.append(_tx(kind="transfer_in" if dep else "transfer_out", quantity=qty, price=price,
                               currency="EUR", amount=0.0, amount_eur=0.0, fee_eur=fee_eur, **base))
        elif _BV_INCOME.search(typ):  # rewards: their value when received becomes their cost (price per unit)
            out.append(_tx(kind="income", quantity=qty if coin != "EUR" else None, price=price, currency="EUR",
                           amount=qty if coin == "EUR" else 0.0, amount_eur=qty if coin == "EUR" else 0.0,
                           fee_eur=0.0, **base))
        else:
            out.append(_tx(kind="other", quantity=qty, price=price, currency=coin, **base))
    return {"format": "bitvavo", "transactions": out, "positions": None, "warnings": sorted(set(warnings))[:20]}


# --- entry point ------------------------------------------------------------------------------

PARSERS = {"degiro_transactions": degiro_transactions, "degiro_account": degiro_account, "ibkr": ibkr,
           "ibkr_flex": ibkr_flex, "bitvavo": bitvavo}


def parse_text(text: str) -> dict:
    fmt = detect(text)
    if fmt == "positions":
        p = parse_positions(text)
        return {"format": p["format"], "transactions": [], "positions": p, "warnings": p["warnings"]}
    res = PARSERS[fmt](text)
    res["transactions"] = [t for t in res["transactions"] if t["date"]]
    return res


def parse_file(path) -> dict:
    from pathlib import Path
    p = Path(path).expanduser()
    if not p.is_file():
        raise ValueError(f"File not found: {p}")
    if p.suffix.lower() in (".xlsx", ".xls"):
        raise ValueError(f"{p.name}: export as CSV (DEGIRO, LYNX and Bitvavo all offer CSV).")
    if p.suffix.lower() == ".pdf":
        try:
            from .pdf_exports import to_csv_text
            res = parse_text(to_csv_text(p)[1])
        except ImportError:
            raise ValueError(f"{p.name}: reading PDFs needs pdfplumber (pip install pdfplumber); "
                             "or export the same report as CSV.") from None
    else:
        res = parse_text(read_text(p))
    res["file"] = p.name
    dates = [t["date"] for t in res["transactions"]]
    res["from"], res["to"] = (min(dates), max(dates)) if dates else (None, None)
    return res
