"""Synthetic Belgian household data (fictional people, merchants are real Belgian brands) for trying centen."""
from __future__ import annotations

import random
from datetime import date, timedelta

from . import db
from .categorize import Context, enrich
from .money import format_ogm


def make_iban(bank_code: str, account_no: int) -> str:
    base = f"{bank_code}{account_no:07d}"
    check = int(base) % 97 or 97
    bban = f"{base}{check:02d}"
    num = int("".join(str(int(c, 36)) for c in bban + "BE00"))
    return f"BE{98 - num % 97:02d}{bban}"


def make_ogm(n: int) -> str:
    base = f"{n:010d}"
    check = int(base) % 97 or 97
    return format_ogm(f"{base}{check:02d}")


def _weekday_before(d: date) -> date:
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def generate(conn, today: date | None = None, months: int = 15, seed: int = 7) -> dict:
    rnd = random.Random(seed)
    today = today or date.today()
    start = (today.replace(day=1) - timedelta(days=31 * (months - 1))).replace(day=1)

    zicht, spaar = make_iban("734", 1234567), make_iban("734", 7654321)
    accounts = [
        dict(id=f"acc:{zicht}", source="demo", bank="KBC", name="Zichtrekening", iban=zicht, account_type="current"),
        dict(id=f"acc:{spaar}", source="demo", bank="KBC", name="Spaarrekening", iban=spaar, account_type="savings"),
        dict(id="demo:visa", source="demo", bank="KBC", name="KBC Visa", iban=None, account_type="card", credit_limit=2500.0),
    ]
    for a in accounts:
        db.upsert_account(conn, **a)
    ctx = Context.from_db(conn)

    txs: list[dict] = []
    seq = [0]

    def add(acc, d, amount, cp=None, cp_iban=None, desc=None, msg=None):
        if d > today or d < start:
            return
        seq[0] += 1
        txs.append(dict(id=db.make_id("demo", seq[0]), account_id=acc, booking_date=d.isoformat(),
                        value_date=d.isoformat(), amount=round(amount, 2), currency="EUR", status="booked",
                        counterparty_name=cp, counterparty_iban=cp_iban, description=desc, remittance=msg,
                        source="demo"))

    Z, S, V = accounts[0]["id"], accounts[1]["id"], accounts[2]["id"]

    def card(d, merchant, city="MECHELEN"):
        return f"BETALING VIA BANCONTACT {d:%d-%m} {rnd.randint(8, 20):02d}:{rnd.randint(0, 59):02d} {merchant} {city} KAART 6703 XXXX XXXX 1234"

    def dd(creditor, cid, mandate):
        return f"EUROPESE DOMICILIERING SCHULDEISER : {creditor} SCHULDEISER-ID : {cid} MANDAATREFERTE : {mandate}"

    def visa(d, merchant):
        return f"AANKOOP KREDIETKAART {d:%d/%m} {merchant}"

    c1, c2 = today - timedelta(days=125), today - timedelta(days=95)
    cut_engie, cut_netflix = (c1.year, c1.month), (c2.year, c2.month)
    d = start
    card_month_total = {}
    while d <= today:
        y, m = d.year, d.month
        month_name = ["januari", "februari", "maart", "april", "mei", "juni", "juli", "augustus", "september",
                      "oktober", "november", "december"][m - 1]
        # income
        pay = _weekday_before(date(y, m, 25))
        add(Z, pay, 3412.86 + rnd.choice([0, 0, 0, 12.4]), "ACME BELGIUM NV", make_iban("310", 1111111),
            "EUROPESE OVERSCHRIJVING VAN", f"Loon {month_name} {y}")
        if m == 5:
            add(Z, date(y, 5, 28), 3104.10, "ACME BELGIUM NV", make_iban("310", 1111111), "EUROPESE OVERSCHRIJVING VAN",
                f"Vakantiegeld {y}")
        if m == 12:
            add(Z, date(y, 12, 18), 2950.00, "ACME BELGIUM NV", make_iban("310", 1111111), "EUROPESE OVERSCHRIJVING VAN",
                f"Eindejaarspremie {y}")
        add(Z, date(y, m, 8), 185.62, "FONS VZW", make_iban("091", 2222222), "EUROPESE OVERSCHRIJVING VAN",
            f"Groeipakket {month_name} {y} kind 1")
        # housing
        add(Z, date(y, m, 1), -1148.37, "KBC BANK NV", make_iban("734", 9999999), "TERUGBETALING WOONKREDIET",
            "Woonkrediet 726-5512340-12 maandelijkse aflossing")
        engie = 145.0 if (y, m) < cut_engie else 162.0
        add(Z, date(y, m, 15), -engie, "ENGIE", None, dd("ENGIE", "BE73ZZZ0403170701", "ENG4417263"),
            f"Voorschot elektriciteit en gas {month_name}")
        add(Z, date(y, m, 5), -89.50, "TELENET BV", None, dd("TELENET BV", "BE29ZZZ0473416418", "TEL00918273"),
            "Telenet One factuur")
        add(Z, date(y, m, 20), -25.00, "PROXIMUS", None, dd("PROXIMUS", "BE56ZZZ0202239951", "PXS7766123"),
            "Mobile abonnement")
        if m in (2, 5, 8, 11):
            add(Z, date(y, m, 12), -78.40, "DE WATERGROEP", None, dd("DE WATERGROEP", "BE81ZZZ0224771467", "DWG551234"),
                "Tussentijdse factuur water")
        if m in (1, 4, 7, 10):
            add(Z, date(y, m, 3), -42.00, "HELAN ONAFHANKELIJK ZIEKENFONDS", None,
                dd("HELAN", "BE63ZZZ0411702543", "HEL33221"), "Ledenbijdrage ziekenfonds kwartaal")
        add(Z, date(y, m, 2), -62.18, "AG INSURANCE", None, dd("AG INSURANCE", "BE34ZZZ0404494849", "AGI882211"),
            "Autoverzekering polis 0123456")
        if m == 3:
            add(Z, date(y, 3, 10), -165.30, "BALOISE BELGIUM NV", make_iban("310", 4444444), "EUROPESE OVERSCHRIJVING NAAR",
                "Familiale verzekering jaarpremie")
        if m == 4:
            add(Z, date(y, 4, 22), -318.56, "VLAAMSE BELASTINGDIENST", make_iban("091", 5555555),
                "EUROPESE OVERSCHRIJVING NAAR", make_ogm(9000000000 + y))
        if m == 9:
            add(Z, date(y, 9, 19), 624.11, "FOD FINANCIEN", make_iban("679", 2005502), "EUROPESE OVERSCHRIJVING VAN",
                f"Teruggave personenbelasting aanslagjaar {y}")
        add(Z, date(y, m, 4), -392.00, "KINDERDAGVERBLIJF DE RUPSJES", make_iban("068", 6666666),
            "EUROPESE OVERSCHRIJVING NAAR", make_ogm(1200000000 + y * 100 + m))
        add(Z, date(y, m, 1), -29.99, "BASIC-FIT BELGIUM", None, dd("BASIC-FIT", "BE11ZZZ0809123456", "BF1029384"),
            "Lidmaatschap")
        add(Z, date(y, m, 28), -2.10, None, None, "BEHEERSKOSTEN REKENING", "Maandelijkse bijdrage pakket Plus")
        # savings & investments
        add(Z, date(y, m, 26), -300.00, "Spaarrekening", spaar, "DOORLOPENDE OPDRACHT NAAR", "Sparen")
        add(S, date(y, m, 26), 300.00, "Zichtrekening", zicht, "DOORLOPENDE OPDRACHT VAN", "Sparen")
        add(Z, date(y, m, 27), -250.00, "flatexDEGIRO Bank AG", None,
            "EUROPESE OVERSCHRIJVING NAAR", "DEGIRO storting")
        # day-to-day
        dd_ = date(y, m, 1)
        while dd_.month == m:
            if dd_.weekday() == 5:
                add(Z, dd_, -rnd.uniform(68, 142), None, None, card(dd_, "COLRUYT"))
            if dd_.weekday() == 2 and rnd.random() < 0.8:
                add(Z, dd_, -rnd.uniform(14, 48), None, None, card(dd_, rnd.choice(["DELHAIZE", "ALDI", "LIDL"])))
            if dd_.weekday() == 4 and rnd.random() < 0.35:
                add(Z, dd_, -rnd.uniform(24, 46), None, None, card(dd_, "FRITUUR DE KROON"))
            if dd_.day % 12 == 3:
                add(Z, dd_, -rnd.uniform(58, 86), None, None, card(dd_, "Q8 BOOISCHOT", "BOOISCHOT"))
            if dd_.day in (6, 21) and rnd.random() < 0.7:
                add(V, dd_, -rnd.uniform(21, 44), None, None, visa(dd_, "DELIVEROO BE"))
            if dd_.day == 14 and rnd.random() < 0.6:
                add(Z, dd_, -rnd.uniform(9, 31), None, None, card(dd_, "KRUIDVAT"))
            if dd_.day == 11 and rnd.random() < 0.5:
                add(Z, dd_, -rnd.uniform(4.5, 12), None, None, card(dd_, "4411 PARKING MECHELEN", "MECHELEN"))
            if dd_.day == 17 and rnd.random() < 0.45:
                add(Z, dd_, -rnd.uniform(12, 38), None, None, card(dd_, "APOTHEEK DE LINDE"))
                add(Z, dd_ + timedelta(days=9), round(rnd.uniform(15, 30), 2), "HELAN ONAFHANKELIJK ZIEKENFONDS",
                    make_iban("068", 7777777), "EUROPESE OVERSCHRIJVING VAN", "Terugbetaling geneeskundige verzorging")
            dd_ += timedelta(days=1)
        add(Z, date(y, m, 10), -50.00, None, None, f"GELDOPNEMING BATOPIN {d:%d-%m} MECHELEN KAART 6703 XXXX XXXX 1234")
        # card subscriptions
        nflx = 13.49 if (y, m) < cut_netflix else 15.99
        add(V, date(y, m, 9), -nflx, None, None, visa(date(y, m, 9), "NETFLIX.COM AMSTERDAM"))
        add(V, date(y, m, 13), -16.99, None, None, visa(date(y, m, 13), "SPOTIFY P2F8A91C STOCKHOLM"))
        add(V, date(y, m, 22), -8.99, None, None, visa(date(y, m, 22), "DISNEY PLUS LONDON"))
        add(V, date(y, m, 3), -1.99, None, None, visa(date(y, m, 3), "GOOGLE *GOOGLE ONE"))
        if rnd.random() < 0.6:
            add(V, date(y, m, 16), -rnd.uniform(18, 75), None, None, visa(date(y, m, 16), "AMAZON.COM.BE"))
        if m == 7:
            add(V, date(y, 7, 2), -642.80, None, None, visa(date(y, 7, 2), "BOOKING.COM AMSTERDAM"))
        nxt_m = date(y + (m == 12), m % 12 + 1, 7)
        card_month_total[(y, m)] = nxt_m
        d = date(y + (m == 12), m % 12 + 1, 1)

    # One-offs in the last weeks that the unusual-activity check should find.
    recent = today - timedelta(days=9)
    add(Z, recent, -38.50, None, None, card(recent, "PIZZERIA DA MARIO"))
    add(Z, recent, -38.50, None, None, card(recent, "PIZZERIA DA MARIO"))
    big = today - timedelta(days=16)
    add(Z, big, -1249.00, "COOLBLUE BELGIE NV", make_iban("001", 8888888), "EUROPESE OVERSCHRIJVING NAAR",
        "Bestelling 9876543 wasmachine")
    add(Z, today - timedelta(days=20), -11.87, None, None, "DEBETRENTE", "Debetintresten periode")

    # Monthly Visa statement settled from the current account.
    for (y, m), settle in card_month_total.items():
        total = -sum(t["amount"] for t in txs if t["account_id"] == V and t["booking_date"][:7] == f"{y}-{m:02d}")
        if total and settle <= today:
            add(Z, settle, -round(total, 2), "KBC BANK NV", None, "AFREKENING KREDIETKAART", "Visa afrekening")

    # Balances consistent with the flows.
    flows = {}
    for t in txs:
        flows[t["account_id"]] = flows.get(t["account_id"], 0.0) + t["amount"]
    opening = {Z: 1250.0, S: 9500.0, V: 0.0}
    for acc, base in opening.items():
        bal = round(base + flows.get(acc, 0.0), 2)
        if acc == V:
            unsettled = sum(t["amount"] for t in txs if t["account_id"] == V and t["booking_date"][:7] == today.isoformat()[:7])
            bal = round(unsettled, 2)
        db.set_balance(conn, acc, today.isoformat(), "booked", bal)
        if acc == Z:
            db.set_balance(conn, acc, today.isoformat(), "available", bal)

    enriched = [enrich(t, ctx) for t in txs]
    n = db.insert_transactions(conn, enriched)
    conn.commit()
    return {"accounts": len(accounts), "transactions": n, "from": start.isoformat(), "to": today.isoformat()}
