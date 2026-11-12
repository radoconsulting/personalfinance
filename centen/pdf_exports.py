"""Convert broker PDF reports to the CSV layout of the same broker's CSV export, so brokers.py reads them.

- DEGIRO "Transacties" PDF  -> Transactions.csv layout
- DEGIRO "Rekeningoverzicht" PDF -> Account.csv layout
- Bitvavo "Transactieoverzicht" PDF -> Bitvavo transaction history CSV layout

Needs pdfplumber (pip install pdfplumber). CSV exports are preferred when the broker offers them.
"""
from __future__ import annotations

import csv
import io
import re
from pathlib import Path

DATE_DMY = re.compile(r"^\d{2}-\d{2}-\d{4}$")
ISIN_RX = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}\d$")
NUM = re.compile(r"^-?[\d.]*\d(,\d+)?$")
CCY = re.compile(r"^[A-Z]{3}$")


def _lines(page, tol=2.5):
    """Words grouped into lines (by vertical position), each word with x0/x1."""
    words = page.extract_words(keep_blank_chars=False, use_text_flow=False, x_tolerance=1.5)
    lines = []
    for w in sorted(words, key=lambda w: (round(w["top"]), w["x0"])):
        if lines and abs(lines[-1]["top"] - w["top"]) <= tol:
            lines[-1]["words"].append(w)
        else:
            lines.append({"top": w["top"], "words": [w]})
    for ln in lines:
        ln["words"].sort(key=lambda w: w["x0"])
        ln["text"] = " ".join(w["text"] for w in ln["words"])
    return lines


def kind(path) -> str | None:
    import pdfplumber
    with pdfplumber.open(str(path)) as pdf:
        head = "\n".join((p.extract_text() or "") for p in pdf.pages[:3])
    if ("Bitvavo" in head and "Transactieoverzicht" in head) or "Received / Paid" in head:
        return "bitvavo"
    if re.search(r"Transacties van \d", head) or ("Uitvoerings" in head and "Wisselkoe" in head):
        return "degiro_transactions"
    if "Rekeningoverzicht" in head or ("Valutadatum" in head and "Omschrijving" in head):
        return "degiro_account"
    return None


def _out(header, rows) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(header)
    w.writerows(rows)
    return buf.getvalue()


# --- DEGIRO transactions ----------------------------------------------------------------------

def degiro_transactions(path) -> str:
    import pdfplumber
    rows = []
    with pdfplumber.open(str(path)) as pdf:
        for page in pdf.pages:
            lines = _lines(page)
            prod_x = None
            for ln in lines:
                ws = ln["words"]
                if ws and ws[0]["text"] == "Datum":
                    prod_x = next((w["x0"] for w in ws if w["text"] == "Product"), prod_x)
                    continue
                if len(ws) >= 2 and DATE_DMY.match(ws[0]["text"]) and re.match(r"^\d\d:\d\d$", ws[1]["text"]):
                    toks = [w["text"] for w in ws]
                    i = next((k for k, t in enumerate(toks) if ISIN_RX.match(t)), None)
                    if i is None:
                        continue
                    product, rest = " ".join(toks[2:i]), toks[i + 1:]
                    cc = [k for k, t in enumerate(rest) if CCY.match(t)]
                    if len(cc) < 2:
                        continue
                    c1, c2 = cc[-2], cc[-1]
                    nums = rest[c2 + 1:]
                    qty, price, local = rest[c1 - 2], rest[c1 - 1], rest[c2 - 1]
                    venue = rest[:c1 - 2]
                    waarde, wk, autofx, kosten, totaal = "", "", "", "", ""
                    if len(nums) >= 5:
                        waarde, wk, autofx, kosten, totaal = nums[:5]
                    elif len(nums) == 4:
                        waarde, wk, autofx, totaal = nums
                    elif len(nums) == 3:
                        waarde, wk, totaal = nums
                    rows.append([toks[0], toks[1], product, toks[i], venue[0] if venue else "",
                                 venue[1] if len(venue) > 1 else "", qty, price, rest[c1], local, rest[c2], waarde,
                                 wk, autofx, kosten, totaal, ""])
                    rows[-1].append(ln["top"])
                elif rows and prod_x is not None and ws and abs(ws[0]["x0"] - prod_x) < 6 and \
                        ln["top"] - rows[-1][-1] < 14 and not any(NUM.match(w["text"]) for w in ws[:1]):
                    part = " ".join(w["text"] for w in ws if w["x0"] < prod_x + 140)
                    rows[-1][2] = f"{rows[-1][2]} {part}".strip()
                    rows[-1][-1] = ln["top"]
    header = ["Datum", "Tijd", "Product", "ISIN", "Beurs", "Uitvoeringsplaats", "Aantal", "Koers", "", "Lokale waarde",
              "", "Waarde EUR", "Wisselkoers", "AutoFX Kosten", "Transactiekosten en/of kosten van derden EUR",
              "Totaal EUR", "Order ID"]
    return _out(header, [r[:-1] for r in rows])


# --- DEGIRO account overview ------------------------------------------------------------------

def degiro_account(path) -> str:
    import pdfplumber
    rows = []
    with pdfplumber.open(str(path)) as pdf:
        for page in pdf.pages:
            cols = None
            for ln in _lines(page):
                ws = ln["words"]
                if ws and ws[0]["text"] == "Datum" and any(w["text"] == "Omschrijving" for w in ws):
                    cols = {w["text"]: w["x0"] for w in ws}
                    continue
                if cols is None or len(ws) < 4 or not DATE_DMY.match(ws[0]["text"]) or \
                        not DATE_DMY.match(ws[2]["text"]):
                    continue
                left = [w for w in ws[3:] if w["x0"] < cols["FX"] - 4]
                right = [w["text"] for w in ws[3:] if w["x0"] >= cols["FX"] - 4]
                product = " ".join(w["text"] for w in left if w["x0"] < cols["ISIN"] - 4)
                isin = " ".join(w["text"] for w in left if cols["ISIN"] - 4 <= w["x0"] < cols["Omschrijving"] - 4)
                desc = " ".join(w["text"] for w in left if w["x0"] >= cols["Omschrijving"] - 4)
                # right side: [FX] [CCY amount] CCY balance
                fx, mc, m, sc, s = "", "", "", "", ""
                r = right[:]
                if len(r) >= 2 and CCY.match(r[-2]):
                    sc, s = r[-2], r[-1]
                    r = r[:-2]
                if len(r) >= 2 and CCY.match(r[-2]):
                    mc, m = r[-2], r[-1]
                    r = r[:-2]
                if r and NUM.match(r[-1]):
                    fx = r[-1]
                    r = r[:-1]
                if r:  # description text that ran into the FX column
                    desc = f"{desc} {' '.join(r)}".strip()
                rows.append([ws[0]["text"], ws[1]["text"], ws[2]["text"], product, isin, desc, fx, mc, m, sc, s, ""])
    header = ["Datum", "Tijd", "Valutadatum", "Product", "ISIN", "Omschrijving", "FX", "Mutatie", "", "Saldo", "",
              "Order Id"]
    return _out(header, rows)


# --- Bitvavo ----------------------------------------------------------------------------------

BV_COLS = ["Type", "Currency", "Amount", "Quote", "Received", "Fee", "Status", "Transaction", "Address"]


def bitvavo(path) -> str:
    import pdfplumber
    out = []
    with pdfplumber.open(str(path)) as pdf:
        for page in pdf.pages:
            lines = _lines(page)
            cols = None
            for ln in lines:
                names = {w["text"]: w["x0"] for w in ln["words"]}
                if "Type" in names and "Currency" in names and "Status" in names:
                    cols = {c: names[c] for c in BV_COLS if c in names}
                    others = [l for l in lines if abs(l["top"] - ln["top"]) < 6 and l is not ln]
                    for o in others:
                        for w in o["words"]:
                            if w["text"] in ("Received", "Fee") and w["text"] not in cols:
                                cols[w["text"]] = w["x0"]
                    break
            if not cols:
                continue
            order = sorted(cols.items(), key=lambda kv: kv[1])

            def col_of(x):
                name = None
                for c, x0 in order:
                    if x >= x0 - 6:
                        name = c
                return name
            data = [l for l in lines if l["words"] and re.match(r"^\d{4}-\d{2}-\d{2}$", l["words"][0]["text"])]
            for i, ln in enumerate(data):
                rec = {c: [] for c in BV_COLS}
                for w in ln["words"][2:]:
                    c = col_of(w["x0"])
                    if c:
                        rec[c].append(w["text"])
                # wrapped cells (type, transaction id) sit just above and below the row
                prev_top = data[i - 1]["top"] if i else -1e9
                next_top = data[i + 1]["top"] if i + 1 < len(data) else 1e9
                for l2 in lines:
                    if l2 is ln or not (ln["top"] - 14 < l2["top"] < ln["top"] + 14):
                        continue
                    if abs(l2["top"] - ln["top"]) > min(ln["top"] - prev_top, next_top - ln["top"]) / 2 + 0.5:
                        continue
                    for w in l2["words"]:
                        c = col_of(w["x0"])
                        if c in ("Type", "Transaction", "Address") and not re.match(r"^\d{4}-", w["text"]):
                            (rec[c].insert(0, w["text"]) if l2["top"] < ln["top"] else rec[c].append(w["text"]))
                q = rec["Quote"]
                rcv = rec["Received"]
                fee = rec["Fee"]
                out.append([ln["words"][0]["text"] + " " + ln["words"][1]["text"], "".join(rec["Type"]),
                            " ".join(rec["Currency"]), " ".join(rec["Amount"]),
                            q[1] if len(q) > 1 else "EUR", q[0] if q else "",
                            rcv[1] if len(rcv) > 1 else "", rcv[0] if rcv else "",
                            fee[1] if len(fee) > 1 else "", fee[0] if fee else "",
                            " ".join(rec["Status"]), "".join(rec["Transaction"]), "".join(rec["Address"])])
    header = ["Date", "Type", "Currency", "Amount", "Quote Currency", "Quote Price", "Received / Paid Currency",
              "Received / Paid Amount", "Fee currency", "Fee amount", "Status", "Transaction ID", "Address"]
    return _out(header, out)


CONVERTERS = {"degiro_transactions": degiro_transactions, "degiro_account": degiro_account, "bitvavo": bitvavo}


def to_csv_text(path) -> tuple[str, str]:
    k = kind(path)
    if not k:
        raise ValueError(f"{Path(path).name}: not a DEGIRO or Bitvavo report I can read; export it as CSV.")
    return k, CONVERTERS[k](path)
