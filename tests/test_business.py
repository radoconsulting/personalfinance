"""Personal / business / total perspectives and money between a company and its owner."""
from datetime import date

import pytest

from centen import analytics as A
from centen import db
from centen.categorize import Context, detect_scope, enrich, recategorize
from centen.demo import make_iban

TODAY = date(2026, 11, 6)
ME = make_iban("063", 1111111)       # personal current account
JOINT = make_iban("750", 2222222)    # joint household account
BV = make_iban("645", 3333333)       # company account
BV_OTHER = make_iban("645", 4444444)  # company savings, not linked
MY_OTHER_BE = make_iban("001", 5555555)  # own-name account, not linked


def iban(country: str, bban: str) -> str:
    num = int("".join(str(int(c, 36)) for c in bban + country + "00"))
    return f"{country}{98 - num % 97:02d}{bban}"


BROKER_DE = iban("DE", "101308000123456789")


def test_detect_scope():
    assert detect_scope("Acme Consulting BV") == "business"
    assert detect_scope("ACME NV") == "business"
    assert detect_scope("Jan Janssens") == "personal"
    assert detect_scope("Janssens - Peeters") == "personal"


@pytest.fixture()
def biz(conn):
    for acc in (dict(id="me", source="demo", bank="Belfius", name="Jan Test", iban=ME),
                dict(id="joint", source="demo", bank="Crelan", name="Test - Partner", iban=JOINT),
                dict(id="bv", source="demo", bank="Bank Van Breda", name="Acme Consulting BV", iban=BV)):
        db.upsert_account(conn, **acc)
    db.set_balance(conn, "me", "2026-11-06", "booked", 5000)
    db.set_balance(conn, "joint", "2026-11-06", "booked", 3000)
    db.set_balance(conn, "bv", "2026-11-06", "booked", 40000)
    n = [0]

    def tx(acc, d, amount, cp=None, cp_iban=None, desc=None, msg=None):
        n[0] += 1
        return dict(id=f"t{n[0]}", account_id=acc, booking_date=d, amount=amount, status="booked", currency="EUR",
                    counterparty_name=cp, counterparty_iban=cp_iban, description=desc, remittance=msg, source="demo")

    txs = [
        tx("bv", "2026-10-02", 12100.0, "Vibe Group Belgium NV", iban("NL", "RABO0123456789"), "Overschrijving",
           "Factuur 2026-10"),
        tx("bv", "2026-10-20", -2100.0, "BTW Ontvangsten", make_iban("679", 2011111), "Overschrijving", "BTW Q3"),
        tx("bv", "2026-10-26", -2400.0, "Jan Test", JOINT, None, "Verloning - 52111 Betaling door uw sociaal secretariaat"),
        tx("joint", "2026-10-26", 2400.0, "Acme Consulting BV", BV, None, "Verloning - 52111"),
        tx("bv", "2026-10-27", -650.0, "SD Worx Sociaal Secretariaat", None, None, "Factuur loonverwerking"),
        tx("bv", "2026-07-01", -10000.0, "Jan Test", ME, None, "Dividend"),
        tx("me", "2026-07-01", 10000.0, "Acme Consulting BV", BV, None, "Dividend"),
        tx("bv", "2026-07-15", -4285.71, "Inningscentrum Roerende Voorheffing", None, None, "Roerende voorheffing dividend"),
        tx("bv", "2026-09-10", -5000.0, "FOD Financien", None, None, "Voorafbetaling vennootschapsbelasting"),
        tx("bv", "2026-10-18", -322.0, "Jan Test", ME, None, "Geindexeerde huur kantoor"),
        tx("me", "2026-10-18", 322.0, "Acme Consulting BV", BV, None, "Geindexeerde huur kantoor"),
        tx("joint", "2026-08-01", -35.0, "Acme Consulting BV", BV, None, "Terugbetaling priveaankoop Amazon"),
        tx("bv", "2026-08-01", 35.0, "Test - Partner", JOINT, None, "Terugbetaling priveaankoop Amazon"),
        tx("bv", "2026-09-02", -15000.0, "Acme Consulting", BV_OTHER, None, "Naar spaarrekening"),
        tx("me", "2026-08-03", -20000.0, "Jan Test", BROKER_DE, "Instant overschrijving", None),
        tx("me", "2026-08-04", -1000.0, "Test Jan", MY_OTHER_BE, "Overschrijving", None),
        tx("joint", "2026-10-05", -120.0, None, None, "BETALING VIA BANCONTACT COLRUYT MECHELEN", None),
    ]
    ctx = Context.from_db(conn)
    db.insert_transactions(conn, [enrich(t, ctx) for t in txs])
    conn.commit()
    return conn


def cat(conn, tid):
    r = conn.execute("SELECT category, kind, internal, merchant FROM transactions WHERE id=?", (tid,)).fetchone()
    return tuple(r)


def test_scopes_assigned(biz):
    scopes = dict(biz.execute("SELECT id, scope FROM accounts").fetchall())
    assert scopes == {"me": "personal", "joint": "personal", "bv": "business"}


def test_company_private_flows(biz):
    assert cat(biz, "t1")[:2] == ("Revenue", "income")
    assert cat(biz, "t2")[:2] == ("VAT", "expense")
    assert cat(biz, "t3")[:3] == ("Director remuneration", "expense", "cross")
    assert cat(biz, "t4")[:3] == ("Director remuneration", "income", "cross")
    assert cat(biz, "t5")[:2] == ("Payroll & social secretariat", "expense")
    assert cat(biz, "t6")[:3] == ("Dividends", "distribution", "cross")
    assert cat(biz, "t7")[:3] == ("Dividends", "income", "cross")
    assert cat(biz, "t8")[:2] == ("Withholding tax", "distribution")
    assert cat(biz, "t9")[:2] == ("Corporate tax", "expense")
    assert cat(biz, "t10")[:2] == ("Rent", "expense")
    assert cat(biz, "t11")[:2] == ("Rental income", "income")
    assert cat(biz, "t12")[:3] == ("Company current account", "transfer", "cross")
    assert cat(biz, "t13")[:3] == ("Company current account", "transfer", "cross")
    assert cat(biz, "t14")[:3] == ("Own accounts", "transfer", "own")  # company's own unlinked account
    inv = cat(biz, "t15")
    assert inv[:3] == ("Savings & investments", "transfer", "own") and inv[3] == "DEGIRO"  # flatexDEGIRO bank code
    assert cat(biz, "t16")[:3] == ("Own accounts", "transfer", "own")  # own name, unlinked Belgian account


def test_perspectives(biz):
    oct_ = (date(2026, 10, 1), date(2026, 10, 31))
    personal = A.breakdown(biz, *oct_, kind="income", perspective="personal")
    assert {i["key"] for i in personal["items"]} == {"Director remuneration", "Rental income"}
    business = A.breakdown(biz, *oct_, perspective="business")
    assert {i["key"]: i["amount"] for i in business["items"]} == {
        "Director remuneration": 2400.0, "VAT": 2100.0, "Payroll & social secretariat": 650.0, "Rent": 322.0}
    total_in = A.breakdown(biz, *oct_, kind="income", perspective="total")
    assert {i["key"] for i in total_in["items"]} == {"Revenue"}  # salary and rent are internal, counted once
    total_out = A.breakdown(biz, *oct_, perspective="total")
    assert "Director remuneration" not in {i["key"] for i in total_out["items"]}
    cf = {m["month"]: m for m in A.cashflow(biz, 6, TODAY, perspective="business")}
    assert cf["2026-07"]["dividends_paid"] == pytest.approx(14285.71)


def test_financial_position(biz):
    pos = A.position(biz, date(2026, 1, 1), TODAY, TODAY)
    b = pos["business"]
    assert b["revenue_incl_vat"] == 12100.0  # the private-purchase repayment is current account, not revenue
    assert b["vat_paid"] == 2100 and b["director_remuneration_net"] == 2400 and b["corporate_tax_paid"] == 5000
    assert b["dividends_paid_net"] == 10000 and b["withholding_tax_on_dividends"] == pytest.approx(4285.71)
    assert b["result_before_corporate_tax"] == pytest.approx(b["revenue_incl_vat"] - 2100 - 2400 - 650 - 322)
    c = pos["company_to_you"]
    assert (c["net_salary"], c["dividends_net"], c["office_rent"]) == (2400, 10000, 322)
    assert pos["cash"] == {**pos["cash"], "personal": 8000, "business": 40000, "total": 48000}
    assert pos["investments"]["net_deposited_total"] == 20000
    assert pos["personal"]["net_put_into_investments"] == 20000


def test_rescope_and_recategorize(biz):
    from centen.categorize import set_scope
    set_scope(biz, "bv", "personal")
    assert cat(biz, "t3")[:2] == ("Own accounts", "transfer")
    set_scope(biz, "bv", "business")
    assert cat(biz, "t3")[:2] == ("Director remuneration", "expense")
    assert recategorize(biz) == 17


def test_business_side_rules():
    ctx = Context(accounts=[{"id": "bv", "iban": BV, "name": "Acme BV", "scope": None}])

    def c(amount, cp, msg=None):
        return enrich(dict(account_id="bv", amount=amount, counterparty_name=cp, remittance=msg), ctx)["category"]

    assert c(106.69, "SD Worx Sociaal Secretariaat", "Verloning correctie") == "Revenue"
    assert c(-783.0, "Xerius Sociaal Verzekeringsfonds", "Sociale bijdrage Q3") == "Taxes & social security"
    assert c(-716.0, "Moore Finance & Tax", "Factuur boekhouding") == "Accounting & advisory"
    assert c(-414.0, "DKV Belgium", None) == "Insurance"
    assert c(-13.8, "Google Workspace Acmec", None) == "Subscriptions & streaming"


def test_warrants_and_gifts(biz):
    from centen.categorize import add_rule
    add_rule(biz, "warrantco invest", "Director remuneration", merchant="Warrants (WarrantCo)")  # your plan provider
    add_rule(biz, "planpay", "Director remuneration", merchant="Warrants (PlanPay)")
    ctx = Context.from_db(biz)
    bv = enrich(dict(account_id="bv", amount=-1559.27, counterparty_name="WarrantCo Investment Solutions",
                     remittance="Warrantenplan oktober"), ctx)
    me = enrich(dict(account_id="me", amount=1434.57, counterparty_name="PlanPay Solutions", remittance="Uitbetaling"), ctx)
    assert (bv["category"], bv["kind"], bv["merchant"]) == ("Director remuneration", "expense", "Warrants (WarrantCo)")
    assert (me["category"], me["kind"], me["merchant"]) == ("Director remuneration", "income", "Warrants (PlanPay)")
    plain = enrich(dict(account_id="bv", amount=-500.0, counterparty_name="Some Provider", remittance="Warrants Q3"), ctx)
    assert plain["category"] == "Director remuneration"
    from centen.categorize import KIND
    assert KIND["Gifts received"] == "income"


def test_household_member_and_flows(biz):
    from centen import config
    cfg = config.load()
    cfg["household_members"] = {"An": ["An Peeters"]}
    config.save(cfg)
    ctx = Context.from_db(biz)
    extra = [
        dict(id="h1", account_id="joint", booking_date="2026-09-30", amount=-500.0, counterparty_name="An Peeters"),
        dict(id="h2", account_id="joint", booking_date="2026-10-13", amount=200.0, counterparty_name="Peeters An"),
        dict(id="h3", account_id="joint", booking_date="2026-09-03", amount=-4.0, counterparty_name="Lies Peeters P2P MOBILE"),
        dict(id="h4", account_id="me", booking_date="2026-10-02", amount=-1000.0, counterparty_name="Test - Partner",
             counterparty_iban=JOINT),
        dict(id="h5", account_id="joint", booking_date="2026-10-02", amount=1000.0, counterparty_name="Jan Test",
             counterparty_iban=ME),
    ]
    for t in extra:
        t.update(status="booked", currency="EUR", source="demo")
    db.insert_transactions(biz, [enrich(t, ctx) for t in extra])
    biz.commit()
    assert cat(biz, "h1")[:4] == ("Household transfers", "transfer", "own", "An")
    assert cat(biz, "h2")[:4] == ("Household transfers", "transfer", "own", "An")
    assert cat(biz, "h3")[1] == "expense"  # another Peeters is not An

    assert A.owner_labels(biz) == {"me": "Jan Test", "joint": "Joint (Test - Partner)", "bv": "Acme Consulting BV"}
    assert A.owner_labels(biz, per_account=True) == {"me": "Jan Test (Belfius)", "joint": "Joint (Crelan)",
                                                     "bv": "Acme Consulting BV"}
    hf = A.household_flows(biz, date(2026, 1, 1), TODAY)
    flows = {(f["from"], f["to"]): f["amount"] for f in hf["flows"]}
    assert flows[("Jan Test (Belfius)", "Joint (Crelan)")] == 1000.0  # counted once, not twice
    assert flows[("Joint (Crelan)", "An")] == 500.0
    assert flows[("An", "Joint (Crelan)")] == 200.0
    assert flows[("Acme Consulting BV", "Joint (Crelan)")] == 2400.0
    assert flows[("Acme Consulting BV", "Jan Test (Belfius)")] == 10322.0  # dividend + office rent
    assert flows[("Jan Test (Belfius)", "Jan Test - other account (not linked)")] == 1000.0
    # a credit from a linked account whose debit side isn't in the data is still counted
    db.insert_transactions(biz, [enrich(dict(id="h6", account_id="me", booking_date="2026-03-01", amount=750.0,
                                             counterparty_name="Test - Partner", counterparty_iban=JOINT,
                                             status="booked", currency="EUR", source="demo"), ctx)])
    biz.commit()
    hf = A.household_flows(biz, date(2026, 1, 1), TODAY)
    flows = {(f["from"], f["to"]): f["amount"] for f in hf["flows"]}
    assert flows[("Joint (Crelan)", "Jan Test (Belfius)")] == 750.0
    assert flows[("Jan Test (Belfius)", "Joint (Crelan)")] == 1000.0
    j = hf["joint_accounts"]["Joint (Crelan)"]
    assert j["transfers_in_from"]["Acme Consulting BV"] == 2400.0 and j["transfers_out_to"]["An"] == 500.0
    assert j["income_paid_in"]["Acme Consulting (Director remuneration)"] == 2400.0
    # household transfers are not spending, but they are listed as transfers
    oct_ = (date(2026, 10, 1), date(2026, 10, 31))
    assert "Household transfers" not in {i["key"] for i in A.breakdown(biz, *oct_)["items"]}
    assert "Household transfers" in {i["key"] for i in A.breakdown(biz, *oct_, kind="transfer")["items"]}
