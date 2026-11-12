import pytest

from centen.money import (clean_iban, eur, find_creditor_id, find_mandate_id, find_structured_ref, is_valid_iban,
                          mask_iban, parse_amount, parse_date)


@pytest.mark.parametrize("raw,expected", [
    ("1.234,56", 1234.56), ("-45,20", -45.20), ("45.20", 45.20), ("€ 12,00", 12.0), ("+1 234,56 EUR", 1234.56),
    ("12,50-", -12.5), ("1,234.56", 1234.56), ("-1.000.000,00", -1000000.0), ("", None), ("abc", None), (3, 3.0),
])
def test_parse_amount(raw, expected):
    assert parse_amount(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("05/11/2026", "2026-11-05"), ("05-11-2026", "2026-11-05"), ("2026-11-05", "2026-11-05"),
    ("05.11.26", "2026-11-05"), ("2026-11-05T10:22:00+01:00", "2026-11-05"), ("31/02/2026", None), ("", None),
])
def test_parse_date(raw, expected):
    assert parse_date(raw) == expected


def test_iban():
    assert is_valid_iban("BE68539007547034")
    assert not is_valid_iban("BE68539007547035")
    assert clean_iban("BE68 5390 0754 7034") == "BE68539007547034"
    assert clean_iban("be68-5390-0754-7034") == "BE68539007547034"
    assert clean_iban("nonsense") is None
    assert mask_iban("BE68539007547034") == "BE…7034"


def test_structured_communication():
    assert find_structured_ref("Betaling +++090/9337/55493+++ factuur") == "+++090/9337/55493+++"
    assert find_structured_ref("***090/9337/55493***") == "+++090/9337/55493+++"
    assert find_structured_ref("090933755493") == "+++090/9337/55493+++"
    assert find_structured_ref("+++090/9337/55494+++") is None  # bad check digits


def test_sepa_ids():
    text = "EUROPESE DOMICILIERING SCHULDEISER : ENGIE SCHULDEISER-ID : BE73ZZZ0403170701 MANDAATREFERTE : ENG4417263"
    assert find_creditor_id(text) == "BE73ZZZ0403170701"
    assert find_mandate_id(text) == "ENG4417263"


def test_eur_format():
    assert eur(1234.5) == "€ 1.234,50"
    assert eur(-0.4) == "-€ 0,40"
