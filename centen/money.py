"""Parsing helpers for Belgian banking data: amounts, dates, IBANs, structured communications."""
from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime

_DATE_FORMATS = (
    "%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%d/%m/%y", "%d-%m-%y", "%d.%m.%y",
    "%Y/%m/%d", "%Y%m%d", "%d %m %Y",
)


def parse_date(value) -> str | None:
    """Return an ISO date string (YYYY-MM-DD) or None. Accepts Belgian (day-first) formats."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    s = str(value).strip()
    if not s:
        return None
    # ISO datetime such as 2026-11-05T10:22:00+01:00
    m = re.match(r"^(\d{4}-\d{2}-\d{2})[T ]", s)
    if m:
        s = m.group(1)
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def parse_amount(value) -> float | None:
    """Parse '1.234,56', '-45,20', '45.20', '€ 12,00', '+1 234,56 EUR' into a float."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    if not s:
        return None
    neg = False
    if s.endswith("-"):  # some exports write 12,50-
        neg, s = True, s[:-1]
    if s.startswith("(") and s.endswith(")"):
        neg, s = True, s[1:-1]
    s = re.sub(r"(?i)eur|€|\s| | ", "", s)
    if s.startswith("+"):
        s = s[1:]
    if s.startswith("-"):
        neg, s = (not neg), s[1:]
    if not s or not re.fullmatch(r"[\d.,']+", s):
        return None
    s = s.replace("'", "")
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        if s.count(",") > 1:
            s = s.replace(",", "")
        else:
            s = s.replace(",", ".")
    elif s.count(".") > 1:
        s = s.replace(".", "")
    try:
        v = float(s)
    except ValueError:
        return None
    return -v if neg else v


def normalize_text(s: str | None) -> str:
    """Lowercase, strip accents, collapse whitespace."""
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", str(s))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", s.lower()).strip()


# --- IBAN -----------------------------------------------------------------

_IBAN_RE = re.compile(r"\b([A-Z]{2}\d{2}(?:\s?[A-Z0-9]{4}){2,7}(?:\s?[A-Z0-9]{1,4})?)\b")


def clean_iban(value: str | None) -> str | None:
    if not value:
        return None
    s = re.sub(r"[\s\-]", "", str(value)).upper()
    # Some exports append the currency: BE68539007547034 EUR
    s = re.sub(r"(EUR|USD|GBP)$", "", s) if len(s) > 18 else s
    return s if is_valid_iban(s) else None


def is_valid_iban(iban: str | None) -> bool:
    if not iban or not re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]{10,30}", iban):
        return False
    if iban.startswith("BE") and len(iban) != 16:
        return False
    rearranged = iban[4:] + iban[:4]
    digits = "".join(str(int(c, 36)) for c in rearranged)
    return int(digits) % 97 == 1


def find_iban(text: str | None) -> str | None:
    if not text:
        return None
    for m in _IBAN_RE.finditer(text.upper()):
        iban = clean_iban(m.group(1))
        if iban:
            return iban
    return None


def mask_iban(iban: str | None) -> str | None:
    """Only the last four characters are ever shown to Claude, e.g. 'BE…7034'."""
    if not iban:
        return None
    return f"{iban[:2]}…{iban[-4:]}"


_IBAN_IN_TEXT = re.compile(r"\b([A-Z]{2}\d{2})((?:\s?[A-Z0-9]{4}){2,7}(?:\s?[A-Z0-9]{1,4})?)\b", re.IGNORECASE)


def mask_ibans_in_text(text: str | None) -> str | None:
    """Replace any IBAN inside free text (bank descriptions often contain them) with its masked form."""
    if not text:
        return text

    def sub(m):
        raw = re.sub(r"\s", "", m.group(0)).upper()
        return f"{raw[:2]}…{raw[-4:]}" if len(raw) >= 15 else m.group(0)

    return _IBAN_IN_TEXT.sub(sub, text)


# --- Belgian structured communication (OGM/VCS: +++123/4567/89002+++) -------

_OGM_RE = re.compile(r"[+*]{3}\s*(\d{3})\s*/?\s*(\d{4})\s*/?\s*(\d{5})\s*[+*]{3}")
_OGM_BARE_RE = re.compile(r"(?<!\d)(\d{3})/(\d{4})/(\d{5})(?!\d)")


def is_valid_ogm(digits: str) -> bool:
    if not re.fullmatch(r"\d{12}", digits):
        return False
    base, check = int(digits[:10]), int(digits[10:])
    mod = base % 97
    return check == (mod if mod else 97)


def format_ogm(digits: str) -> str:
    return f"+++{digits[:3]}/{digits[3:7]}/{digits[7:]}+++"


def find_structured_ref(text: str | None) -> str | None:
    """Return a validated Belgian structured communication, formatted +++xxx/xxxx/xxxxx+++."""
    if not text:
        return None
    for rx in (_OGM_RE, _OGM_BARE_RE):
        for m in rx.finditer(text):
            digits = "".join(m.groups())
            if is_valid_ogm(digits):
                return format_ogm(digits)
    bare = re.sub(r"\D", "", text)
    if len(bare) == 12 and is_valid_ogm(bare):
        return format_ogm(bare)
    return None


# --- SEPA direct debit identifiers ------------------------------------------

_CREDITOR_ID_RE = re.compile(r"\b([A-Z]{2}\d{2}ZZZ[A-Z0-9]{5,28}|BE\d{2}[A-Z0-9]{3}\d{10})\b")
_MANDATE_RE = re.compile(
    r"(?i)(?:mandaat|mandat|mandate)\s*(?:referte|referentie|reference|ref|nummer|nr|n°|id)?\s*[:.]?\s*"
    r"([A-Z0-9][A-Z0-9\-/]{4,34})"
)


def find_creditor_id(text: str | None) -> str | None:
    if not text:
        return None
    m = _CREDITOR_ID_RE.search(text.upper())
    return m.group(1) if m else None


def find_mandate_id(text: str | None) -> str | None:
    if not text:
        return None
    m = _MANDATE_RE.search(text)
    return m.group(1).upper() if m else None


def eur(amount: float | None) -> str:
    """Belgian formatting: € 1.234,56"""
    if amount is None:
        return "—"
    sign = "-" if amount < 0 else ""
    whole, frac = f"{abs(amount):,.2f}".split(".")
    return f"{sign}€ {whole.replace(',', '.')},{frac}"
