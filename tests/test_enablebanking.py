import json
from datetime import date

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from centen import enablebanking as eb
from centen.demo import make_iban

APP_ID = "11111111-2222-3333-4444-555555555555"
IBAN = make_iban("734", 1234567)


@pytest.fixture(scope="module")
def keys():
    k = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    return pem, k.public_key()


class FakeBank:
    """Minimal stand-in for api.enablebanking.com: checks the JWT and serves paged transactions."""

    def __init__(self, public_key):
        self.pub = public_key
        self.calls = []
        self.pending_amount = "12.00"

    def __call__(self, request: httpx.Request) -> httpx.Response:
        token = request.headers["Authorization"].split()[1]
        assert jwt.get_unverified_header(token)["kid"] == APP_ID
        jwt.decode(token, self.pub, algorithms=["RS256"], audience="api.enablebanking.com", issuer="enablebanking.com")
        path, q = request.url.path, dict(request.url.params)
        self.calls.append((request.method, path, q))
        if path == "/aspsps":
            return httpx.Response(200, json={"aspsps": [{"name": "KBC", "country": "BE", "maximum_consent_validity": 15552000},
                                                        {"name": "KBC Brussels", "country": "BE"}]})
        if path == "/auth":
            body = json.loads(request.content)
            assert body["aspsp"] == {"name": "KBC", "country": "BE"} and body["psu_type"] == "personal"
            return httpx.Response(200, json={"url": f"https://tilisy.enablebanking.com/auth?state={body['state']}"})
        if path == "/sessions" and request.method == "POST":
            assert json.loads(request.content) == {"code": "the-code"}
            return httpx.Response(200, json={"session_id": "sess-1", "access": {"valid_until": "2027-05-01T00:00:00+00:00"},
                                             "aspsp": {"name": "KBC", "country": "BE"},
                                             "accounts": [{"uid": "uid-1", "account_id": {"iban": IBAN}, "name": "Zichtrekening",
                                                           "currency": "EUR", "cash_account_type": "CACC",
                                                           "identification_hash": "h1"}]})
        if path == "/accounts/uid-1/balances":
            return httpx.Response(200, json={"balances": [
                {"balance_type": "CLBD", "balance_amount": {"amount": "1500.00", "currency": "EUR"}, "reference_date": "2026-11-06"},
                {"balance_type": "ITAV", "balance_amount": {"amount": "1488.00", "currency": "EUR"}, "reference_date": "2026-11-06"}]})
        if path == "/accounts/uid-1/transactions":
            if "continuation_key" not in q:
                return httpx.Response(200, json={"continuation_key": "page2", "transactions": [
                    {"entry_reference": "E1", "transaction_amount": {"amount": "87.30", "currency": "EUR"},
                     "credit_debit_indicator": "DBIT", "status": "BOOK", "booking_date": "2026-11-05",
                     "remittance_information": ["BETALING VIA BANCONTACT COLRUYT MECHELEN"],
                     "bank_transaction_code": {"code": "PMNT", "sub_code": "CCRD-POSD"}},
                    {"transaction_amount": {"amount": "3412.86", "currency": "EUR"}, "credit_debit_indicator": "CRDT",
                     "status": "BOOK", "booking_date": "2026-10-24", "debtor": {"name": "ACME BELGIUM NV"},
                     "debtor_account": {"iban": "BE71096123456769"}, "remittance_information": ["Loon oktober"]}]})
            return httpx.Response(200, json={"transactions": [
                {"transaction_amount": {"amount": self.pending_amount, "currency": "EUR"}, "credit_debit_indicator": "DBIT",
                 "status": "PDNG", "booking_date": "2026-11-06", "remittance_information": ["DELHAIZE"]},
                {"transaction_amount": {"amount": "1.00", "currency": "EUR"}, "credit_debit_indicator": "DBIT",
                 "status": "RJCT", "booking_date": "2026-11-06"}]})
        if path == "/sessions/sess-1" and request.method == "DELETE":
            return httpx.Response(200, json={"message": "OK"})
        return httpx.Response(404, json={"message": f"unexpected {path}"})


def make_client(keys, bank):
    return eb.Client(APP_ID, keys[0], transport=httpx.MockTransport(bank))


CFG = {"enablebanking": {"app_id": APP_ID, "private_key_path": "x", "redirect_url": "http://127.0.0.1:8765/callback",
                         "country": "BE", "api_base": "https://api.enablebanking.com"}, "min_sync_hours": 6}


def test_link_and_sync(conn, keys):
    bank = FakeBank(keys[1])
    client = make_client(keys, bank)
    started = eb.start_link(conn, CFG, "kbc", client=client)
    assert started["bank"] == "KBC" and started["url"].startswith("https://")
    res = eb.complete_link(conn, CFG, f"http://127.0.0.1:8765/callback?state={started['state']}&code=the-code",
                           client=client)
    assert res["accounts"][0]["iban"] == IBAN[:2] + "…" + IBAN[-4:]
    acc = conn.execute("SELECT * FROM accounts").fetchone()
    assert (acc["id"], acc["account_type"], acc["external_uid"]) == (f"acc:{IBAN}", "current", "uid-1")

    s = eb.sync(conn, CFG, client=client, today=date(2026, 11, 6))
    assert s["errors"] == [] and s["accounts"][0]["new_transactions"] == 3  # 2 booked + 1 pending, rejected dropped
    rows = sorted(tuple(r) for r in conn.execute("SELECT category, status, amount FROM transactions"))
    assert rows == [("Groceries", "booked", -87.3), ("Groceries", "pending", -12.0), ("Salary", "booked", 3412.86)]
    assert conn.execute("SELECT payment_type FROM transactions WHERE category='Groceries'").fetchone()[0] == "card"
    bal = dict(conn.execute("SELECT balance_type, amount FROM balances").fetchall())
    assert bal == {"booked": 1500.0, "available": 1488.0}
    # first sync asks for up to two years of history
    tx_call = [c for c in bank.calls if c[1].endswith("/transactions")][0]
    assert tx_call[2]["date_from"] == "2024-11-06"

    # A second sync inside min_sync_hours is skipped; forced sync replaces pending items instead of duplicating.
    assert "skipped" in eb.sync(conn, CFG, client=client, today=date(2026, 11, 6))["accounts"][0]
    bank.pending_amount = "12.50"
    eb.sync(conn, CFG, client=client, force=True, today=date(2026, 11, 6))
    pend = conn.execute("SELECT amount FROM transactions WHERE status='pending'").fetchall()
    assert [p[0] for p in pend] == [-12.5]
    assert conn.execute("SELECT COUNT(*) FROM transactions WHERE status='booked'").fetchone()[0] == 2
    # incremental sync starts 10 days before the newest booked transaction
    last_tx_call = [c for c in bank.calls if c[1].endswith("/transactions")][-2]
    assert last_tx_call[2]["date_from"] == "2026-10-26"


def test_expired_consent(conn, keys):
    conn.execute("INSERT INTO bank_links(id, aspsp_name, country, session_id, valid_until, status) "
                 "VALUES ('s','KBC','BE','s','2026-01-01T00:00:00+00:00','active')")
    s = eb.sync(conn, CFG, client=make_client(keys, FakeBank(keys[1])), today=date(2026, 11, 6))
    assert "expired" in s["errors"][0]
    assert conn.execute("SELECT status FROM bank_links").fetchone()[0] == "expired"


def test_ambiguous_bank_name(conn, keys):
    with pytest.raises(ValueError):
        eb.find_aspsp(make_client(keys, FakeBank(keys[1])), "Bank of Nowhere")


def test_redirect_parsing():
    assert eb.code_from_redirect("https://x/cb?state=abc&code=xyz") == ("xyz", "abc")
    assert eb.code_from_redirect("xyz") == ("xyz", None)
    with pytest.raises(ValueError):
        eb.code_from_redirect("https://x/cb?error=access_denied&state=abc")
