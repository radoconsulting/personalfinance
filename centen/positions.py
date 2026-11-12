"""Read holdings from broker exports.

- DEGIRO: Portfolio → export (Portfolio.csv). Columns like Product, Symbool/ISIN, Aantal, Slotkoers, Lokale waarde,
  Waarde in EUR (Dutch, English and French headers; comma or semicolon separated).
- LYNX / Interactive Brokers: an Activity Statement exported as CSV (sections "Open Positions", "Net Asset Value",
  "Base Currency Exchange Rate", "Financial Instrument Information").
- Anything else: a table with at least a name and a value column (name/naam/product, isin, aantal/quantity,
  koers/price, waarde/value [in EUR]).

Returns plain dicts; assets.set_positions stores them.
"""
from __future__ import annotations

import csv
import io
import re
from datetime import datetime
from pathlib import Path

from .money import normalize_text, parse_amount

ISIN = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}\d$")
CCY = re.compile(r"^[A-Z]{3}$")


def read_text(path: str | Path) -> str:
    raw = Path(path).expanduser().read_bytes()
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def parse_file(path: str | Path) -> dict:
    p = Path(path).expanduser()
    if not p.is_file():
        raise ValueError(f"File not found: {p}")
    if p.suffix.lower() in (".xlsx", ".xls", ".pdf"):
        raise ValueError("Export the positions as CSV (DEGIRO: Portfolio → Export → CSV; LYNX: Activity Statement as "
                         "CSV), or give the positions to Claude to enter with set_positions.")
    return parse_text(read_text(p))


def parse_text(text: str) -> dict:
    if re.search(r"^Open Positions,Header,", text, re.M):
        return _ibkr(text)
    return _table(text)


# --- plain tables (DEGIRO and generic) --------------------------------------------------------

_NAME = ("product", "produit", "produkt", "naam", "name", "omschrijving", "instrument", "description", "security",
         "effect", "fonds", "fund", "asset", "coin", "titre", "libelle")
_QTY = ("aantal", "quantity", "quantite", "qty", "hoeveelheid", "units", "stuks", "anzahl", "aantal stuks", "nombre",
        "balance", "saldo")
_PRICE = ("slotkoers", "koers", "closing", "close", "cloture", "price", "prijs", "kurs", "last", "laatste koers",
          "current price", "marktprijs", "cours", "schlusskurs")
_VALUE = ("waarde", "value", "marktwaarde", "market value", "valeur", "total value", "totale waarde", "wert", "bedrag",
          "huidige waarde", "current value")
_LOCAL = ("lokale waarde", "local value", "valeur locale", "lokaler wert", "waarde lokaal")
_CCY = ("currency", "munt", "valuta", "devise", "wahrung", "ccy")
_COST = ("kostprijs", "cost basis", "costbasis", "aankoopwaarde", "total cost", "invested", "inleg", "geinvesteerd",
         "aankoopbedrag", "cost")
_SYMBOL = ("symbol", "symbool", "ticker", "symbole", "code")


def _h(s: str) -> str:
    return re.sub(r"\s+", " ", normalize_text(s or "")).strip()


def _num(s) -> float | None:
    if s is None:
        return None
    s = str(s).strip().replace(" ", "")
    if not s or s in ("-", "—"):
        return None
    return parse_amount(s)


def _columns(header: list[str]) -> dict:
    cols = {}
    for i, raw in enumerate(header):
        h = _h(raw)
        if not h:
            continue
        if "isin" in h:
            cols.setdefault("isin", i)
        elif h in _LOCAL:
            cols.setdefault("local", i)
        elif ("eur" in h.split() or h.endswith("eur") or "(eur)" in h or "in eur" in h) and \
                any(w in h for w in ("waarde", "value", "valeur", "wert", "bedrag")):
            cols["value_eur"] = i
        elif h in _VALUE:
            cols.setdefault("value", i)
        elif h in _NAME:
            cols.setdefault("name", i)
        elif h in _QTY:
            cols.setdefault("quantity", i)
        elif h in _PRICE:
            cols.setdefault("price", i)
        elif h in _CCY:
            cols.setdefault("currency", i)
        elif h in _COST:
            cols.setdefault("cost", i)
        elif h in _SYMBOL:
            cols.setdefault("symbol", i)
    return cols


def _table(text: str) -> dict:
    first = next((l for l in text.splitlines() if l.strip()), "")
    delim = max([";", ",", "\t"], key=first.count)
    rows = list(csv.reader(io.StringIO(text), delimiter=delim))
    hi, cols = None, {}
    for i, r in enumerate(rows[:15]):
        c = _columns(r)
        if ("name" in c or "isin" in c) and ({"value_eur", "value", "local", "quantity"} & set(c)):
            hi, cols = i, c
            break
    if hi is None:
        raise ValueError("Couldn't find the columns: the file needs a header with at least a name (Product / Naam) and "
                         "a value (Waarde in EUR / Value) or quantity column.")
    fmt = "degiro" if any("symbool/isin" in _h(x) or "symbol/isin" in _h(x) or "symbole/isin" in _h(x)
                          for x in rows[hi]) else "table"
    out, warnings = [], []
    for r in rows[hi + 1:]:
        if not any(x.strip() for x in r):
            continue
        get = lambda k: r[cols[k]].strip() if k in cols and cols[k] < len(r) else None  # noqa: E731
        name = get("name")
        code = get("isin") or get("symbol")
        if not name and not code:
            continue
        if _h(name or "") in ("totaal", "total", "totaux", "summe", "gesamt"):
            continue
        isin = code if code and ISIN.match(code.upper()) else None
        symbol = get("symbol") if "symbol" in cols else (code if code and not isin else None)
        currency = get("currency")
        local = None
        if "local" in cols:
            a = get("local")
            nxt = r[cols["local"] + 1].strip() if cols["local"] + 1 < len(r) else ""
            if a and CCY.match(a.upper()) and _num(nxt) is not None:
                currency, local = currency or a.upper(), _num(nxt)  # DEGIRO: "EUR","1234,56" in two columns
            else:
                local = _num(a)
        value = _num(get("value_eur")) if "value_eur" in cols else None
        if value is None and "value" in cols:
            v = _num(get("value"))
            if v is not None and (not currency or currency.upper() == "EUR"):
                value = v
            elif v is not None:
                local = local if local is not None else v
        if value is None and local is not None and (not currency or currency.upper() == "EUR"):
            value = local
        qty, price = _num(get("quantity")), _num(get("price"))
        if value is None and qty is not None and price is not None and (not currency or currency.upper() == "EUR"):
            value = qty * price
        if value is None:
            warnings.append(f"no EUR value for {name or code}")
        out.append({"name": name or code, "isin": isin, "symbol": symbol, "quantity": qty, "price": price,
                    "currency": (currency or "EUR").upper() if currency or value is not None else None,
                    "value_local": local, "value": value, "cost": _num(get("cost"))})
    if not out:
        raise ValueError("The file has a header but no positions.")
    return {"format": fmt, "positions": out, "as_of": None, "total_value": None, "warnings": warnings}


# --- Interactive Brokers / LYNX activity statement --------------------------------------------

def _ibkr_date(s: str) -> str | None:
    s = (s or "").strip()
    end = s.split(" - ")[-1].strip()
    for fmt in ("%B %d, %Y", "%Y-%m-%d", "%d %B %Y", "%b %d, %Y", "%Y%m%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(end, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _ibkr(text: str) -> dict:
    sections: dict[str, list[dict]] = {}
    headers: dict[str, list[str]] = {}
    for r in csv.reader(io.StringIO(text)):
        if len(r) < 3:
            continue
        sec, kind = r[0], r[1]
        if kind == "Header":
            headers[sec] = r[2:]
        elif kind in ("Data", "Total", "SubTotal") and sec in headers:
            sections.setdefault(sec, []).append({"_kind": kind, **dict(zip(headers[sec], r[2:]))})
    base, as_of = "EUR", None
    for row in sections.get("Account Information", []):
        if row.get("Field Name") == "Base Currency":
            base = (row.get("Field Value") or "EUR").strip().upper()
    for row in sections.get("Statement", []):
        if row.get("Field Name") in ("Period", "WhenGenerated"):
            as_of = as_of or _ibkr_date(row.get("Field Value", ""))
    fx = {base: 1.0}
    for row in sections.get("Base Currency Exchange Rate", []):
        rate = _num(row.get("Rate"))
        if row.get("Currency") and rate:
            fx[row["Currency"].strip().upper()] = rate
    info = {}
    for row in sections.get("Financial Instrument Information", []):
        if row.get("Symbol"):
            info[row["Symbol"].strip()] = {"name": (row.get("Description") or "").strip() or None,
                                           "isin": (row.get("Security ID") or "").strip() or None}
    out, warnings = [], []
    for row in sections.get("Open Positions", []):
        if row["_kind"] != "Data" or row.get("DataDiscriminator", "Summary") not in ("Summary", ""):
            continue
        sym = (row.get("Symbol") or "").strip()
        ccy = (row.get("Currency") or base).strip().upper()
        local = _num(row.get("Value"))
        rate = fx.get(ccy)
        value = local * rate if local is not None and rate and base == "EUR" else None
        cost = _num(row.get("Cost Basis"))
        meta = info.get(sym, {})
        isin = meta.get("isin") if meta.get("isin") and ISIN.match(meta["isin"]) else None
        if value is None:
            warnings.append(f"no EUR rate for {sym} ({ccy})")
        cat = normalize_text(row.get("Asset Category") or "")
        out.append({"name": meta.get("name") or sym, "symbol": sym, "isin": isin,
                    "quantity": _num(row.get("Quantity")), "price": _num(row.get("Close Price")), "currency": ccy,
                    "value_local": local, "value": None if value is None else round(value, 2),
                    "cost": round(cost * rate, 2) if cost is not None and rate and base == "EUR" else None,
                    "asset_class": "etf" if "etf" in cat else "bond" if "bond" in cat else
                                   "fund" if "fund" in cat else None})
    total = cash = None
    for row in sections.get("Net Asset Value", []):
        cls = (row.get("Asset Class") or "").strip().lower()
        cur = _num(row.get("Current Total"))
        if cls == "total" and cur is not None:
            total = cur
        elif cls == "cash" and cur is not None:
            cash = cur
    if cash and base == "EUR":
        out.append({"name": "Cash", "symbol": None, "isin": None, "quantity": None, "price": None, "currency": "EUR",
                    "value_local": cash, "value": round(cash, 2), "cost": None, "asset_class": "cash"})
    if not out:
        raise ValueError("No open positions found in this statement.")
    if base != "EUR":
        warnings.append(f"base currency is {base}: values can't be converted to EUR")
        total = None
    return {"format": "ibkr", "positions": out, "as_of": as_of, "total_value": total, "warnings": warnings}
