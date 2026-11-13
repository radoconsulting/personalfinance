"""Local SQLite store. Nothing leaves this file except what a tool returns to Claude."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from . import config
from .money import mask_iban

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id            TEXT PRIMARY KEY,
    source        TEXT NOT NULL,              -- enablebanking | csv | camt053 | demo | manual | asset
    bank          TEXT,
    name          TEXT,
    iban          TEXT,                       -- full IBAN, never returned to Claude
    iban_masked   TEXT,                       -- BE…1234
    currency      TEXT DEFAULT 'EUR',
    account_type  TEXT DEFAULT 'current',     -- current | savings | card | loan | other; assets (source 'asset'):
                                              -- investment | crypto | pension | insurance | property | asset | loan
    scope         TEXT,                       -- personal | business (auto-detected from the name)
    credit_limit  REAL,
    external_uid  TEXT,                       -- Enable Banking account uid
    link_id       TEXT,
    created_at    TEXT DEFAULT (datetime('now')),
    updated_at    TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS balances (
    account_id    TEXT NOT NULL,
    as_of         TEXT NOT NULL,
    balance_type  TEXT NOT NULL,              -- booked | available | ISO code | manual | valuation | contributions
    amount        REAL NOT NULL,
    currency      TEXT DEFAULT 'EUR',
    PRIMARY KEY (account_id, as_of, balance_type)
);

CREATE TABLE IF NOT EXISTS transactions (
    id                 TEXT PRIMARY KEY,
    account_id         TEXT NOT NULL,
    booking_date       TEXT NOT NULL,
    value_date         TEXT,
    amount             REAL NOT NULL,         -- signed: negative = money out
    currency           TEXT DEFAULT 'EUR',
    status             TEXT DEFAULT 'booked', -- booked | pending
    counterparty_name  TEXT,
    counterparty_iban  TEXT,                  -- never returned to Claude
    counterparty_masked TEXT,
    description        TEXT,
    remittance         TEXT,                  -- free or structured message (mededeling)
    structured_ref     TEXT,                  -- +++123/4567/89002+++
    payment_type       TEXT,                  -- card | direct_debit | transfer | standing_order | instant | cash | fee | other
    mandate_id         TEXT,
    creditor_id        TEXT,
    bank_tx_code       TEXT,
    mcc                TEXT,
    merchant           TEXT,
    merchant_key       TEXT,
    category           TEXT,
    category_source    TEXT,                  -- rule | user | default
    kind               TEXT,                  -- expense | income | transfer | distribution
    internal           TEXT,                  -- own (between own accounts) | cross (company <-> private)
    asset_id           TEXT,                  -- the investment / pension / loan account this payment goes to or comes from
    source             TEXT,
    raw_json           TEXT,                  -- never returned to Claude
    imported_at        TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_tx_date ON transactions(booking_date);
CREATE INDEX IF NOT EXISTS ix_tx_account ON transactions(account_id, booking_date);
CREATE INDEX IF NOT EXISTS ix_tx_merchant ON transactions(merchant_key);
CREATE INDEX IF NOT EXISTS ix_tx_category ON transactions(category);

CREATE TABLE IF NOT EXISTS categories (
    name   TEXT PRIMARY KEY,
    kind   TEXT NOT NULL,
    grp    TEXT
);

CREATE TABLE IF NOT EXISTS category_rules (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern    TEXT NOT NULL,                 -- case/accent-insensitive regex or plain text
    direction  TEXT DEFAULT 'any',            -- any | debit | credit
    category   TEXT NOT NULL,
    merchant   TEXT,                          -- optional display name to assign
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS bank_links (
    id           TEXT PRIMARY KEY,
    aspsp_name   TEXT,
    country      TEXT,
    session_id   TEXT,
    valid_until  TEXT,
    status       TEXT,
    created_at   TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS sync_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id  TEXT,
    started_at  TEXT,
    finished_at TEXT,
    status      TEXT,
    message     TEXT,
    n_new       INTEGER
);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

-- Investment, crypto, pension, insurance, property and loan accounts you add yourself (accounts.source = 'asset').
-- Payments to them are recognised by the counterparty account or a text pattern.
CREATE TABLE IF NOT EXISTS asset_links (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id   TEXT NOT NULL,
    kind         TEXT NOT NULL,               -- iban | match
    iban         TEXT,                        -- full IBAN, never returned to Claude
    iban_masked  TEXT,
    pattern      TEXT                         -- matched case/accent-insensitively against counterparty, description, message
);
CREATE INDEX IF NOT EXISTS ix_asset_links ON asset_links(account_id);

-- Holdings per account and date (from a broker export or entered by hand). value is in EUR.
CREATE TABLE IF NOT EXISTS positions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id   TEXT NOT NULL,
    as_of        TEXT NOT NULL,
    name         TEXT NOT NULL,
    isin         TEXT,
    symbol       TEXT,
    asset_class  TEXT,                        -- etf | equity | fund | bond | crypto | cash | other
    quantity     REAL,
    price        REAL,
    currency     TEXT,
    value_local  REAL,
    value        REAL,
    cost         REAL,
    source       TEXT
);
CREATE INDEX IF NOT EXISTS ix_positions ON positions(account_id, as_of);

-- Transactions from broker exports (DEGIRO, LYNX/IBKR, Bitvavo): trades, dividends, costs, taxes, deposits.
-- amount / amount_eur: cash effect on the broker account (+ = money in); fee_eur: costs of a trade (positive).
CREATE TABLE IF NOT EXISTS inv_transactions (
    id           TEXT PRIMARY KEY,            -- stable per row: importing the same export twice adds nothing
    account_id   TEXT NOT NULL,
    date         TEXT NOT NULL,
    time         TEXT,
    kind         TEXT NOT NULL,               -- buy | sell | dividend | dividend_tax | interest | fee | tax | deposit |
                                              -- withdrawal | transfer_in | transfer_out | income | other
    name         TEXT,
    isin         TEXT,
    symbol       TEXT,
    quantity     REAL,
    price        REAL,
    currency     TEXT,
    amount       REAL,
    amount_eur   REAL,
    fee_eur      REAL,
    fx           REAL,
    order_id     TEXT,
    description  TEXT,
    source       TEXT,                        -- degiro_transactions | degiro_account | ibkr | ibkr_flex | bitvavo
    file         TEXT,
    imported_at  TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_inv_tx ON inv_transactions(account_id, date);

-- Logged-in browsers of the server (r22): only a hash of the cookie is stored; key_fp ties them to the access key,
-- so changing the key logs everyone out.
CREATE TABLE IF NOT EXISTS web_sessions (
    id_hash    TEXT PRIMARY KEY,
    key_fp     TEXT NOT NULL,
    created    TEXT NOT NULL,
    last_seen  TEXT NOT NULL,
    agent      TEXT
);
"""

# Columns and tables the read-only SQL tool may never see.
HIDDEN_COLUMNS = {("accounts", "iban"), ("transactions", "counterparty_iban"), ("transactions", "raw_json"),
                  ("accounts", "external_uid"), ("asset_links", "iban")}
HIDDEN_TABLES = {"bank_links", "meta", "web_sessions"}


# Schema version, stored in PRAGMA user_version. Bump it when a table, column or index is added: connect() then sets the
# database up once; at the same version a connection does no writes at all. Steps in _STEPS run once, in order, for
# databases older than their version (for changes CREATE ... IF NOT EXISTS can't express).
SCHEMA_VERSION = 22

_COLUMNS = {"accounts": [("scope", "TEXT")], "transactions": [("internal", "TEXT"), ("asset_id", "TEXT")]}


def _add_columns(conn: sqlite3.Connection) -> None:
    for table, cols in _COLUMNS.items():
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for col, typ in cols:
            if col not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")


def _replaced_links(conn: sqlite3.Connection) -> None:
    """r22: links whose accounts all moved to a newer link of the same bank are 'replaced', not 'active'."""
    conn.execute("""UPDATE bank_links SET status='replaced' WHERE status='active'
                    AND NOT EXISTS (SELECT 1 FROM accounts a WHERE a.link_id = bank_links.id)
                    AND EXISTS (SELECT 1 FROM bank_links n WHERE n.aspsp_name = bank_links.aspsp_name
                                AND n.id <> bank_links.id AND n.created_at >= bank_links.created_at)""")


_STEPS = [(22, _replaced_links)]


def all_schemas() -> str:
    from .analysis import SCHEMA as ANALYSIS
    from .depreciation import SCHEMA as DEPRECIATION
    from .fixed import SCHEMA as FIXED
    from .history import SCHEMA as HISTORY
    from .loans import SCHEMA as LOANS
    from .networth import SCHEMA as NETWORTH
    from .prices import SCHEMA as PRICES
    return "\n".join([SCHEMA, PRICES, HISTORY, NETWORTH, ANALYSIS, LOANS, FIXED, DEPRECIATION])


def _setup_stamp() -> str:
    from .categorize import CATEGORIES, RULES_VERSION
    h = hashlib.sha256(json.dumps(CATEGORIES).encode()).hexdigest()[:12]
    return f"{SCHEMA_VERSION}|{RULES_VERSION}|{h}"


def _setup(conn: sqlite3.Connection, version: int, new: bool) -> None:
    from .categorize import CATEGORIES, RULES_VERSION, detect_scope, recategorize  # local import: avoid a cycle
    conn.executescript(all_schemas())
    _add_columns(conn)
    conn.execute("CREATE INDEX IF NOT EXISTS ix_tx_asset ON transactions(asset_id)")
    for v, step in _STEPS:
        if not new and version < v:
            step(conn)
    conn.executemany("INSERT OR REPLACE INTO categories(name, kind, grp) VALUES (?,?,?)", CATEGORIES)
    for r in conn.execute("SELECT id, name FROM accounts WHERE scope IS NULL").fetchall():
        conn.execute("UPDATE accounts SET scope=? WHERE id=?", (detect_scope(r["name"]), r["id"]))
    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    set_meta(conn, "setup", _setup_stamp())
    conn.commit()
    if get_meta(conn, "rules_version") != RULES_VERSION and \
            conn.execute("SELECT 1 FROM transactions LIMIT 1").fetchone():
        recategorize(conn)  # rules changed since the data was categorised


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    path = Path(path) if path else config.db_path()
    new = not path.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    stamp = None
    if version == SCHEMA_VERSION:
        try:
            stamp = get_meta(conn, "setup")
        except sqlite3.OperationalError:
            stamp = None
    if version != SCHEMA_VERSION or stamp != _setup_stamp():
        if conn.execute("PRAGMA journal_mode").fetchone()[0].lower() != "wal":
            conn.execute("PRAGMA journal_mode=WAL")
        _setup(conn, version, new)
        try:
            path.chmod(0o600)
        except OSError:
            pass
    return conn


def connect_readonly(path: Path | str | None = None) -> sqlite3.Connection:
    """Read-only connection with an authorizer: SELECT only, sensitive columns read as NULL."""
    path = Path(path) if path else config.db_path()
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    allowed = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION}
    rec = getattr(sqlite3, "SQLITE_RECURSIVE", None)
    if rec is not None:
        allowed.add(rec)

    def authorizer(action, arg1, arg2, dbname, source):
        if action not in allowed:
            return sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_READ:
            if arg1 in HIDDEN_TABLES:
                return sqlite3.SQLITE_DENY
            if (arg1, arg2) in HIDDEN_COLUMNS:
                return sqlite3.SQLITE_IGNORE
        if action == sqlite3.SQLITE_FUNCTION and arg2 and arg2.lower() in {"load_extension", "readfile", "writefile"}:
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    conn.set_authorizer(authorizer)
    return conn


def make_id(*parts) -> str:
    h = hashlib.sha256("\x1f".join("" if p is None else str(p) for p in parts).encode("utf-8"))
    return h.hexdigest()[:24]


def upsert_account(conn: sqlite3.Connection, **acc) -> str:
    acc.setdefault("iban_masked", mask_iban(acc.get("iban")))
    acc["currency"] = acc.get("currency") or "EUR"
    if not acc.get("scope") and not conn.execute("SELECT scope FROM accounts WHERE id=? AND scope IS NOT NULL",
                                                 (acc["id"],)).fetchone():
        from .categorize import detect_scope
        acc["scope"] = detect_scope(acc.get("name"))
    cols = ["id", "source", "bank", "name", "iban", "iban_masked", "currency", "account_type", "scope",
            "credit_limit", "external_uid", "link_id"]
    vals = [acc.get(c) for c in cols]
    conn.execute(
        f"""INSERT INTO accounts({','.join(cols)}) VALUES ({','.join('?' * len(cols))})
            ON CONFLICT(id) DO UPDATE SET
              bank=COALESCE(excluded.bank, bank), name=COALESCE(excluded.name, name),
              iban=COALESCE(excluded.iban, iban), iban_masked=COALESCE(excluded.iban_masked, iban_masked),
              currency=COALESCE(excluded.currency, currency), account_type=COALESCE(excluded.account_type, account_type),
              scope=COALESCE(scope, excluded.scope),
              credit_limit=COALESCE(excluded.credit_limit, credit_limit),
              external_uid=COALESCE(excluded.external_uid, external_uid), link_id=COALESCE(excluded.link_id, link_id),
              updated_at=datetime('now')""",
        vals,
    )
    return acc["id"]


TX_COLS = ["id", "account_id", "booking_date", "value_date", "amount", "currency", "status",
           "counterparty_name", "counterparty_iban", "counterparty_masked", "description", "remittance",
           "structured_ref", "payment_type", "mandate_id", "creditor_id", "bank_tx_code", "mcc",
           "merchant", "merchant_key", "category", "category_source", "kind", "internal", "asset_id", "source", "raw_json"]


def insert_transactions(conn: sqlite3.Connection, txs: list[dict]) -> int:
    """Insert new transactions; existing ids are left untouched (keeps user categories). Returns # new."""
    before = conn.total_changes
    rows = []
    for t in txs:
        t = dict(t)
        t.setdefault("counterparty_masked", mask_iban(t.get("counterparty_iban")))
        if isinstance(t.get("raw_json"), (dict, list)):
            t["raw_json"] = json.dumps(t["raw_json"], ensure_ascii=False, default=str)
        rows.append([t.get(c) for c in TX_COLS])
    conn.executemany(
        f"INSERT OR IGNORE INTO transactions({','.join(TX_COLS)}) VALUES ({','.join('?' * len(TX_COLS))})", rows
    )
    return conn.total_changes - before


def set_balance(conn, account_id: str, as_of: str, balance_type: str, amount: float, currency: str = "EUR"):
    conn.execute(
        "INSERT OR REPLACE INTO balances(account_id, as_of, balance_type, amount, currency) VALUES (?,?,?,?,?)",
        (account_id, as_of, balance_type, amount, currency),
    )


def own_ibans(conn) -> set[str]:
    return {r[0] for r in conn.execute("SELECT iban FROM accounts WHERE iban IS NOT NULL")}


def asset_ibans(conn) -> set[str]:
    return {r[0] for r in conn.execute("SELECT iban FROM asset_links WHERE iban IS NOT NULL")}


def get_meta(conn, key, default=None):
    r = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return r[0] if r else default


def set_meta(conn, key, value):
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?,?)", (key, value))
