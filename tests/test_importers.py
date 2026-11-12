from centen.demo import make_iban
from centen.importers import import_file

KBC = make_iban("734", 1234567)
OTHER = "BE71096123456769"


def write(tmp_path, name, text, encoding="utf-8"):
    p = tmp_path / name
    p.write_bytes(text.encode(encoding))
    return p


def test_kbc_csv(conn, tmp_path):
    f = write(tmp_path, "kbc.csv", f"""Rekeningnummer;Rubrieknaam;Naam;Munt;Afschriftnummer;Datum;Omschrijving;Valuta;Bedrag;Saldo;credit;debet;rekeningnummer tegenpartij;BIC tegenpartij;Naam tegenpartij;Adres tegenpartij;gestructureerde mededeling;Vrije mededeling
{KBC};Zichtrekening;JANSSENS J;EUR;2026011;05/11/2026;BETALING VIA BANCONTACT 05-11 14:02 COLRUYT MECHELEN KAART 6703 XXXX XXXX 1234;05/11/2026;-87,30;1.512,70;;87,30;;;;;;
{KBC};Zichtrekening;JANSSENS J;EUR;2026011;04/11/2026;EUROPESE OVERSCHRIJVING NAAR;04/11/2026;-392,00;1.600,00;;392,00;{OTHER};GKCCBEBB;KINDERDAGVERBLIJF DE RUPSJES;;090933755493;
{KBC};Zichtrekening;JANSSENS J;EUR;2026010;25/10/2026;EUROPESE OVERSCHRIJVING VAN;25/10/2026;3.412,86;1.992,00;3.412,86;;BE10310111111104;BBRUBEBB;ACME BELGIUM NV;;;Loon oktober 2026
""", "cp1252")
    res = import_file(conn, f)
    assert res["new_transactions"] == 3
    rows = {r["category"]: dict(r) for r in conn.execute("SELECT * FROM transactions")}
    assert set(rows) == {"Groceries", "Childcare & school", "Salary"}
    assert rows["Childcare & school"]["structured_ref"] == "+++090/9337/55493+++"
    assert rows["Childcare & school"]["counterparty_iban"] == OTHER
    assert rows["Groceries"]["value_date"] == "2026-11-05"  # 'Valuta' recognised as a date column here
    acc = conn.execute("SELECT * FROM accounts").fetchone()
    assert acc["bank"] == "KBC" and acc["iban"] == KBC
    bal = conn.execute("SELECT amount, as_of FROM balances").fetchone()
    assert (bal["amount"], bal["as_of"]) == (1512.70, "2026-11-05")
    # Re-importing the same (or an overlapping) export adds nothing.
    assert import_file(conn, f)["new_transactions"] == 0


def test_bnp_csv_skips_refused(conn, tmp_path):
    acc = make_iban("001", 1234567)
    f = write(tmp_path, "bnp.csv", f"""Volgnummer;Uitvoeringsdatum;Valutadatum;Bedrag;Valuta rekening;Rekeningnummer;Type verrichting;Tegenpartij;Naam van de tegenpartij;Mededeling;Details;Status;Reden van weigering
2026-0101;03/11/2026;03/11/2026;-45,20;EUR;{acc};Kaartbetaling;;;;BETALING MET DEBETKAART NUMMER 4871 04XX XXXX 1234 DELHAIZE ZAVENTEM 03/11;Geaccepteerd;
2026-0102;04/11/2026;04/11/2026;-10,00;EUR;{acc};Kaartbetaling;;;;BETALING MET DEBETKAART LIDL;Geweigerd;Onvoldoende saldo
2026-0103;05/11/2026;05/11/2026;-89,50;EUR;{acc};Europese domiciliëring;{OTHER};TELENET;Factuur 123;EUROPESE DOMICILIERING SCHULDEISER-ID BE29ZZZ0473416418 MANDAATREFERTE TEL00918273;Geaccepteerd;
""")
    res = import_file(conn, f)
    assert res["new_transactions"] == 2
    t = conn.execute("SELECT * FROM transactions WHERE category='Telecom & internet'").fetchone()
    assert t["payment_type"] == "direct_debit" and t["mandate_id"] == "TEL00918273"
    assert t["counterparty_iban"] == OTHER  # 'Tegenpartij' recognised as an IBAN column here
    assert conn.execute("SELECT bank FROM accounts").fetchone()[0] == "BNP Paribas Fortis"


def test_ing_csv_combines_detail_columns(conn, tmp_path):
    acc = make_iban("310", 7654321)
    f = write(tmp_path, "ing.csv", f"""Rekeningnummer;Naam van de rekening;Rekening tegenpartij;Omzetnummer;Boekingsdatum;Valutadatum;Bedrag;Munteenheid;Omschrijving;Detail van de omzet;Bericht
{acc};ING Lion Account;;00123;02/11/2026;02/11/2026;-62,30;EUR;Betaling met debetkaart;Q8 BOOISCHOT 02/11 08:12;
""")
    import_file(conn, f)
    t = conn.execute("SELECT * FROM transactions").fetchone()
    assert t["category"] == "Fuel" and t["merchant"] == "Q8"
    assert conn.execute("SELECT name FROM accounts").fetchone()[0] == "ING Lion Account"


def test_belfius_csv_with_preamble(conn, tmp_path):
    acc = make_iban("068", 1111111)
    f = write(tmp_path, "belfius.csv", f"""Rekening;{acc};;
Van;01/10/2026;;
Tot;06/11/2026;;
;;;
Rekening;Boekingsdatum;Rekeninguittrekselnummer;Transactienummer;Rekening tegenpartij;Naam tegenpartij bevat;Straat en nummer;Postcode en plaats;Transactie;Valutadatum;Bedrag;Devies;BIC;Landcode;Mededelingen
{acc};15/10/2026;10;0001;;ENGIE;;;EUROPESE DOMICILIERING VAN ENGIE MANDAAT ENG4417263;15/10/2026;-162,00;EUR;;BE;Voorschot oktober
""")
    import_file(conn, f)
    t = conn.execute("SELECT * FROM transactions").fetchone()
    assert (t["category"], t["amount"], t["booking_date"]) == ("Energy", -162.0, "2026-10-15")
    assert conn.execute("SELECT bank FROM accounts").fetchone()[0] == "Belfius"


def test_argenta_csv_dash_dates(conn, tmp_path):
    acc = make_iban("979", 1234567)
    f = write(tmp_path, "argenta.csv", f"""Nr v/d rekening;Boekdatum;Valuta datum;Referentie;Beschrijving;Bedrag;Munt;Verrichtingsdatum;Rekening tegenpartij;Naam tegenpartij;Mededeling
{acc};08-11-2026;08-11-2026;B6K08AB12345;Overschrijving;185,62;EUR;08-11-2026;{OTHER};FONS VZW;Groeipakket november
""")
    import_file(conn, f)
    t = conn.execute("SELECT * FROM transactions").fetchone()
    assert (t["category"], t["booking_date"]) == ("Child benefit (Groeipakket)", "2026-11-08")
    assert conn.execute("SELECT bank FROM accounts").fetchone()[0] == "Argenta"


def test_csv_without_account_column_uses_name(conn, tmp_path):
    f = write(tmp_path, "visa.csv", """Datum,Omschrijving,Bedrag
2026-11-01,NETFLIX.COM AMSTERDAM,-15.99
""")
    import_file(conn, f, account_name="KBC Visa", account_type="card")
    acc = conn.execute("SELECT * FROM accounts").fetchone()
    assert (acc["id"], acc["account_type"]) == ("csv:kbc-visa", "card")


CAMT = f"""<?xml version="1.0" encoding="UTF-8"?>
<Document xmlns="urn:iso:std:iso:20022:tech:xsd:camt.053.001.02">
 <BkToCstmrStmt><Stmt>
  <Acct><Id><IBAN>{KBC}</IBAN></Id><Ccy>EUR</Ccy></Acct>
  <Bal><Tp><CdOrPrtry><Cd>CLBD</Cd></CdOrPrtry></Tp><Amt Ccy="EUR">1512.70</Amt><CdtDbtInd>CRDT</CdtDbtInd><Dt><Dt>2026-11-05</Dt></Dt></Bal>
  <Ntry><Amt Ccy="EUR">162.00</Amt><CdtDbtInd>DBIT</CdtDbtInd><Sts>BOOK</Sts>
   <BookgDt><Dt>2026-11-05</Dt></BookgDt><ValDt><Dt>2026-11-05</Dt></ValDt><AcctSvcrRef>REF123</AcctSvcrRef>
   <BkTxCd><Domn><Cd>PMNT</Cd><Fmly><Cd>RDDT</Cd><SubFmlyCd>ESDD</SubFmlyCd></Fmly></Domn></BkTxCd>
   <NtryDtls><TxDtls><Refs><MndtId>ENG4417263</MndtId></Refs>
    <RltdPties><Cdtr><Nm>ENGIE</Nm><Id><PrvtId><Othr><Id>BE73ZZZ0403170701</Id></Othr></PrvtId></Id></Cdtr>
     <CdtrAcct><Id><IBAN>{OTHER}</IBAN></Id></CdtrAcct></RltdPties>
    <RmtInf><Ustrd>Voorschot november</Ustrd></RmtInf></TxDtls></NtryDtls>
  </Ntry>
  <Ntry><Amt Ccy="EUR">20.00</Amt><CdtDbtInd>CRDT</CdtDbtInd><Sts>PDNG</Sts><BookgDt><Dt>2026-11-06</Dt></BookgDt>
   <NtryDtls><TxDtls><RltdPties><Dbtr><Nm>HELAN</Nm></Dbtr></RltdPties><RmtInf><Ustrd>Terugbetaling</Ustrd></RmtInf></TxDtls></NtryDtls>
  </Ntry>
 </Stmt></BkToCstmrStmt>
</Document>"""


def test_camt053(conn, tmp_path):
    f = write(tmp_path, "stmt.xml", CAMT)
    res = import_file(conn, f)
    assert res["new_transactions"] == 2
    e = conn.execute("SELECT * FROM transactions WHERE amount < 0").fetchone()
    assert (e["category"], e["payment_type"], e["creditor_id"], e["mandate_id"]) == \
        ("Energy", "direct_debit", "BE73ZZZ0403170701", "ENG4417263")
    assert conn.execute("SELECT status FROM transactions WHERE amount > 0").fetchone()[0] == "pending"
    assert conn.execute("SELECT amount FROM balances WHERE balance_type='booked'").fetchone()[0] == 1512.70


def test_camt_rejects_entities(conn, tmp_path):
    import pytest
    f = write(tmp_path, "evil.xml", '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><Document/>')
    with pytest.raises(ValueError):
        import_file(conn, f)


def test_cross_source_dedupe(conn, tmp_path):
    f1 = write(tmp_path, "stmt.xml", CAMT)
    import_file(conn, f1)
    f2 = write(tmp_path, "kbc.csv", f"""Rekeningnummer;Datum;Omschrijving;Bedrag
{KBC};05/11/2026;EUROPESE DOMICILIERING ENGIE;-162,00
{KBC};05/11/2026;BETALING COLRUYT;-10,00
""")
    res = import_file(conn, f2)
    assert res["skipped_as_synced_elsewhere"] == 1 and res["new_transactions"] == 1
