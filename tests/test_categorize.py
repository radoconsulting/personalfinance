import pytest

from centen.categorize import Context, detect_payment_type, enrich
from centen.money import normalize_text


def cat(amount, desc=None, cp=None, msg=None, cp_iban=None, ctx=None, account_id="a1"):
    tx = {"amount": amount, "description": desc, "counterparty_name": cp, "remittance": msg,
          "counterparty_iban": cp_iban, "account_id": account_id}
    return enrich(tx, ctx or Context())


@pytest.mark.parametrize("amount,desc,cp,msg,expected,merchant", [
    (-87.3, "BETALING VIA BANCONTACT 03-11 14:32 COLRUYT MECHELEN KAART 6703 XXXX XXXX 1234", None, None,
     "Groceries", "Colruyt"),
    (-34.1, "PAIEMENT PAR CARTE BANCAIRE DELHAIZE BRUXELLES", None, None, "Groceries", "Delhaize"),
    (-162.0, "EUROPESE DOMICILIERING", "ENGIE", "Voorschot elektriciteit en gas", "Energy", "Engie"),
    (-78.4, "DOMICILIATION EUROPEENNE", "VIVAQUA", None, "Water", "Vivaqua"),
    (-89.5, None, "TELENET BV", "factuur", "Telecom & internet", "Telenet"),
    (-42.0, None, "HELAN ONAFHANKELIJK ZIEKENFONDS", "Ledenbijdrage", "Health insurance (mutualiteit)", None),
    (23.5, None, "CHRISTELIJKE MUTUALITEIT", "Terugbetaling geneeskundige verzorging", "Healthcare refunds", None),
    (185.6, None, "FONS VZW", "Groeipakket november", "Child benefit (Groeipakket)", "Groeipakket"),
    (3400, None, "ACME NV", "Loon oktober 2026", "Salary", None),
    (624.1, None, "FOD FINANCIEN", "Teruggave personenbelasting", "Tax refund", "FOD Financiën"),
    (-318.6, None, "VLAAMSE BELASTINGDIENST", "+++090/9337/55493+++", "Taxes & social security", "Vlaamse Belastingdienst"),
    (-250, None, "flatexDEGIRO Bank AG", "storting", "Savings & investments", "DEGIRO"),
    (-100, None, "Interactive Brokers Ireland", None, "Savings & investments", "LYNX"),
    (-15.99, "AANKOOP KREDIETKAART 09/11 NETFLIX.COM AMSTERDAM", None, None, "Subscriptions & streaming", "Netflix"),
    (-16.99, "PAYPAL *SPOTIFY", "PayPal Europe", None, "Subscriptions & streaming", "Spotify"),
    (-62.3, "BETALING MET DEBETKAART Q8 BOOISCHOT", None, None, "Fuel", "Q8"),
    (-21.0, "ALLEGO LAADSESSIE", None, None, "EV charging", None),
    (-2.5, "4411 PARKING MECHELEN", None, None, "Parking & tolls", "4411"),
    (-11.87, "DEBETRENTE", None, None, "Bank fees", None),
    (-50, "GELDOPNEMING BATOPIN MECHELEN", None, None, "Cash withdrawal", None),
    (-29.99, "SEPA DIRECT DEBIT", "BASIC-FIT BELGIUM", None, "Leisure & sport", "Basic-Fit"),
    (-392, None, "KINDERDAGVERBLIJF DE RUPSJES", "+++120/2026/11xx+++", "Childcare & school", None),
    (-1148.37, "TERUGBETALING WOONKREDIET", "KBC BANK NV", None, "Loans & mortgage", None),
    (-950, "DOORLOPENDE OPDRACHT", "J. Peeters", "Huur december", "Rent", None),
    (-12.0, "BETALING VIA BANCONTACT APOTHEEK DE LINDE", None, None, "Healthcare", None),
    (-1249, None, "COOLBLUE BELGIE NV", "Bestelling wasmachine", "Electronics", "Coolblue"),
    (-14.6, "BETALING VIA BANCONTACT NMBS GENT-SINT-PIETERS", None, None, "Public transport & taxi", "NMBS"),
    (-5.0, "Something nobody knows", None, None, "Other expenses", None),
    (5.0, "Something nobody knows", None, None, "Other income", None),
])
def test_rules(amount, desc, cp, msg, expected, merchant):
    tx = cat(amount, desc, cp, msg)
    assert tx["category"] == expected, (tx["category"], tx["merchant"])
    if merchant:
        assert tx["merchant"] == merchant


def test_own_account_transfer_and_card_bill():
    ctx = Context(own_ibans={"BE68539007547034"}, has_card_account=True, card_accounts={"card1"})
    tx = cat(-300, "DOORLOPENDE OPDRACHT", "Spaarrekening", "Sparen", cp_iban="BE68539007547034", ctx=ctx)
    assert (tx["category"], tx["kind"]) == ("Own accounts", "transfer")
    bill = cat(-212.4, "AFREKENING KREDIETKAART", "KBC BANK NV", "Visa afrekening", ctx=ctx)
    assert (bill["category"], bill["kind"]) == ("Credit card bill", "transfer")
    # Without a linked card, the settlement is the only record of that spending.
    bill2 = cat(-212.4, "AFREKENING KREDIETKAART", "KBC BANK NV", "Visa afrekening")
    assert (bill2["category"], bill2["kind"]) == ("Credit card bill", "expense")
    # A purchase on the card account itself is not a card bill.
    buy = cat(-30, "AANKOOP KREDIETKAART 13/10 SOMEWHERE NEW", None, None, ctx=ctx, account_id="card1")
    assert buy["category"] != "Credit card bill"


def test_user_rules_take_precedence():
    ctx = Context(user_rules=[{"pattern": "colruyt", "direction": "debit", "category": "Shopping", "merchant": "Colruyt"}])
    assert cat(-10, "BETALING VIA BANCONTACT COLRUYT MECHELEN", ctx=ctx)["category"] == "Shopping"


def test_refund_from_merchant_offsets_category():
    tx = cat(49.99, None, "ZALANDO SE", "Retour bestelling")
    assert (tx["category"], tx["kind"]) == ("Clothing & shoes", "expense")


@pytest.mark.parametrize("text,code,expected", [
    ("europese domiciliering engie", None, "direct_debit"),
    ("betaling via bancontact colruyt", None, "card"),
    ("geldopneming batopin", None, "cash"),
    ("doorlopende opdracht naar", None, "standing_order"),
    ("europese overschrijving naar", None, "transfer"),
    ("whatever", "PMNT-RDDT-ESDD", "direct_debit"),
    ("whatever", "PMNT-CCRD-POSD", "card"),
])
def test_payment_type(text, code, expected):
    assert detect_payment_type(normalize_text(text), code) == expected
