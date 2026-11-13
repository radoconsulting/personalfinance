"""MCP server: the Personal Finance tools Claude uses. Run with `centen serve` (stdio)."""
from __future__ import annotations

import json
import os
import time
from contextlib import closing
from datetime import date, timedelta
from typing import Literal, Optional

from typing_extensions import TypedDict

try:  # MCP Python SDK v2
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # v1
    from mcp.server.fastmcp import FastMCP as _Server

try:
    from mcp.types import ToolAnnotations
    READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
    LOCAL_EDIT = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False)
    BANK_READ = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True)
    LOCAL_DELETE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False)
except Exception:  # pragma: no cover
    READ = LOCAL_EDIT = BANK_READ = LOCAL_DELETE = None

from . import analytics as A
from . import categorize, config, db
from .money import mask_ibans_in_text

Perspective = Literal["personal", "business", "total"]

INSTRUCTIONS = """\
centen gives you read-only access to the user's Belgian bank and card accounts, synced through PSD2 open banking
(Enable Banking) or imported from their bank's CSV / CAMT.053 exports. Everything lives in a local SQLite file on
the user's own computer.

Three perspectives:
- personal: the user and their household (own and joint accounts).
- business: accounts of the user's company (e.g. a BV), detected from the account name; change with set_account_scope.
- total: everything together, with money between the company and the user (salary, dividends, office rent,
  current account) counted once.
Household members (e.g. a partner whose own account isn't linked) are set with set_household_member; transfers with
them are "Household transfers": not spending or income, but visible. `household_flows` shows who moved money to
whom (own accounts, the joint account, household members, the company).
For "my total financial situation" use `financial_position`. Money between the company and the user shows up as
Director remuneration / Dividends / Rental income on the personal side and as a cost (salary) or a distribution
(dividends, withholding tax) on the business side. Business figures are cash basis from bank data, not accounting
results; say so when it matters.
Savings accounts are usually not shared through open banking: `unlinked_own_accounts` finds them from transfers
and `add_manual_account` adds one with a balance the user gives; centen then estimates it from later transfers.
Investment accounts (DEGIRO, LYNX, Bolero, funds), crypto (Bitvavo, a Ledger wallet), pension savings
(pensioensparen, VAPZ, IPT / groepsverzekering), tak 21/23 contracts, a home and loans never come through the bank
link either. `list_assets` shows the ones added and suggests brokers and pension-like premiums seen in the payments;
`add_asset_account` adds one with the value the user gives (from the broker app, a yearly statement, the loan table)
and takes over its transfers, which add up to the contributions (inleg). Values are estimates between updates: the
last value plus money moved in or out since, so ask for a fresh value (`set_asset_value`) when it's old, or read
holdings with `import_positions` (DEGIRO Portfolio.csv, LYNX/IBKR activity statement CSV) or `set_positions` (e.g.
from a screenshot the user shares). Contributions made before the bank history starts can be set once with
`update_asset_account(contributions=..., contributions_date=...)`. Net worth (financial_position.net_worth) = cash +
investments + property − loans. Gains are value minus contributions, not tax figures.
Portfolio management: `import_broker_export` reads the exports of DEGIRO (Transactions.csv, Account.csv,
Portfolio.csv), LYNX / Interactive Brokers (activity statement or Flex Query CSV) and Bitvavo (transaction history
CSV), files or a whole folder, safely re-importable. `portfolio` then gives holdings with average cost (GAK), cost,
value, unrealised and realised results, dividends, withholding tax, costs, stock-exchange tax (TOB), deposits per
year and the money-weighted return (XIRR); `portfolio_transactions` lists the underlying rows. With a full broker
history, its deposits replace the bank transfers as contributions. These are management figures, not a tax return.
Live prices: `refresh_prices` (also automatic on sync and build_dashboard, when older than 15 minutes) values holdings
at current prices (Yahoo Finance by ISIN, Bitvavo for crypto) with today's change; `set_price_symbol` fixes a quote.
`build_dashboard` writes a local HTML dashboard (data folder/dashboard.html) for a visual overview; it is also
rebuilt after every sync. Point the user to it when they want to see the picture rather than read it.

r15 additions: `tax_overview` (Belgian capital gains tax from 2026, dividends/RV, TOB, Reynders, a checklist for the
tax return; `set_reference_price` for a missing 31/12/2025 price), `net_worth_history` (month-ends, estimated where
bank history is missing), `uncategorised_payments` + `label_merchants` (clear out 'Other' per merchant),
`portfolio_analysis` + `set_allocation_targets` (allocation vs target, rebalancing, dividend calendar, benchmark),
`weekly_digest`, `import_bank_history` (older bank history from CSV/CAMT/CODA files), and `set_loan_terms` +
`property_and_loans` (a mortgage's repayment plan: amount owed kept up to date, interest vs capital, equity).
When the user wants to update figures themselves, `open_update_app` opens a local form app for it. The dashboard has a tab
"Fiscaal" for the tax view.

r17 additions: `fixed_costs` (subscriptions and fixed costs grouped, price increases, stopped payments, overlapping
services; adjust with `set_fixed_cost`, add missing ones with `add_fixed_cost`), `cash_forecast` (expected balances for
the coming months with low points and warnings), `pension_overview` + `set_pension_profile` (the four pillars, projected
to the pension date), `scenario_plan` (wealth projection and financial independence with what-if scenarios) and
`phone_notifications` (ntfy push to the phone). Dashboard tabs: "Vaste kosten", "Prognose", "Pensioen", "Planner".
r18: `remuneration_vs_dividend` + `set_tax_profile`, `add_vehicle` / `set_depreciation` (a car at a depreciating value
in the net worth), `loan_prepayment` (pay off or invest), `import_pension_statement` (read the insurer's pension fiche).
r19: `possessions` (home, cars, watches: value, purchase price, change, running costs). 'asset' accounts are possessions:
they count in the net worth but not in the investments' value or return. Dashboard tab "Bezittingen".

How to help:
- Answer in the user's language (often Dutch or French) and in everyday words. Explain Belgian banking terms when
  they matter (domiciliëring = direct debit, gestructureerde mededeling, voorschotfactuur, debetrente, Groeipakket).
- Work from their real numbers. For broad questions start with `overview`; then use the specific tools. Never invent
  transactions or balances. If data is missing or stale (see `data_up_to` / `data_status`), say so.
- Amounts: negative = money out. Write euros the Belgian way (€ 1.234,56) unless the user writes otherwise.
- Access is read-only: you cannot pay, transfer, cancel or invest anything. When the user wants to act (cancel a
  subscription, stop a domiciliëring, move money), tell them where to do it in their banking app.
- This is not investment, tax or legal advice. Explain options and trade-offs; for their tax return point to MyMinfin
  or an accountant; if money is tight, mention free debt counselling (schuldhulpverlening via OCMW/CAW).
- Figures can lag the bank (pending items, balance dates). For decisions that matter, suggest checking the bank app.
- Charts, bill calendars and dashboards: get the numbers with these tools, then build the visual yourself. Use
  `query_sql` for custom aggregations.
- Privacy: tools only show the last 4 characters of account numbers, also inside descriptions. Never ask for full
  IBANs, card numbers, passwords or card-reader codes.
- Categories are rule-based. If something is wrong, offer `recategorize_transaction` or `add_category_rule`; they only
  change the local copy, never anything at the bank.
"""

server = _Server("centen", instructions=INSTRUCTIONS)


def _tool(annotations):
    try:
        return server.tool(annotations=annotations) if annotations is not None else server.tool()
    except TypeError:  # very old SDKs
        return server.tool()


def _today() -> date:
    t = os.environ.get("CENTEN_TODAY")
    return date.fromisoformat(t) if t else date.today()


def _conn():
    return closing(db.connect())


def _out(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


def _date(s: Optional[str], default: date) -> date:
    return date.fromisoformat(s[:10]) if s else default


@_tool(READ)
def overview(perspective: Perspective = "personal") -> str:
    """Start here for broad money questions ("how am I doing this month?", "how is the company doing?").
    Returns balances, this month's spending (costs) and income with top categories, last month at the same point,
    bills due in the next 14 days, alerts, and how fresh the data is, for the chosen perspective."""
    with _conn() as c:
        return _out(A.overview(c, _today(), perspective))


@_tool(READ)
def financial_position(date_from: Optional[str] = None, date_to: Optional[str] = None) -> str:
    """The complete picture across personal and business: net worth (cash + investments + property − loans), cash
    per scope and per account, investments (value per account where the user entered one, contributions, gain;
    brokers not added yet at net deposits), loans, the company's cash-basis result (revenue,
    VAT, director salary, other costs, corporate tax, dividends paid), personal income and spending, and what moved
    from the company to the user. Period defaults to this calendar year to date."""
    today = _today()
    with _conn() as c:
        return _out(A.position(c, _date(date_from, today.replace(month=1, day=1)), _date(date_to, today), today))


@_tool(READ)
def household_flows(date_from: Optional[str] = None, date_to: Optional[str] = None, by_month: bool = False) -> str:
    """Who moved money to whom in a period (default: this year): between your own accounts, to and from the joint
    account, with household members (e.g. your partner's own account) and from your company (salary, dividends,
    rent, current account). Includes, per joint account, where its money came from and went to. Investments are
    left out (see financial_position)."""
    today = _today()
    with _conn() as c:
        return _out(A.household_flows(c, _date(date_from, today.replace(month=1, day=1)), _date(date_to, today),
                                      by_month))


@_tool(READ)
def list_accounts() -> str:
    """All connected accounts with scope (personal / business), bank, masked account number (last 4 only),
    booked and available balance, balance date, credit limit, last sync and history range."""
    with _conn() as c:
        from . import assets
        return _out({"accounts": A.accounts_overview(c), "totals": A.totals(c),
                     "investment_and_other_accounts": [
                         {k: a.get(k) for k in ("name", "type", "provider", "scope", "value", "outstanding",
                                                "value_as_of", "estimated", "stale")}
                         for a in assets.list_accounts(c, _today())],
                     "note": "Cash accounts above; investments, pensions, property and loans: list_assets."})


@_tool(READ)
def search_transactions(query: Optional[str] = None, category: Optional[str] = None, merchant: Optional[str] = None,
                        account: Optional[str] = None, date_from: Optional[str] = None, date_to: Optional[str] = None,
                        min_amount: Optional[float] = None, max_amount: Optional[float] = None,
                        direction: Optional[Literal["in", "out"]] = None, limit: int = 50) -> str:
    """Find transactions. `query` matches merchant, counterparty, description, message (mededeling), category or
    structured communication. Dates are YYYY-MM-DD; amounts compare on absolute value; direction 'out' = money
    spent. Returns the match count and sum, plus up to `limit` (max 500) most recent matches."""
    with _conn() as c:
        return _out(A.search(c, query, category, merchant, account, date_from, date_to, min_amount, max_amount,
                             direction, limit))


@_tool(READ)
def spending_breakdown(date_from: Optional[str] = None, date_to: Optional[str] = None,
                       group_by: Literal["category", "group", "merchant", "month", "week", "account"] = "category",
                       kind: Literal["expense", "income", "distribution", "transfer"] = "expense",
                       account: Optional[str] = None,
                       perspective: Perspective = "personal") -> str:
    """Totals by category, category group, merchant, month, week or account for a period (default: this month to
    date). Spending (business: costs) is positive here; refunds offset it. Transfers between own accounts, to
    savings/brokers and credit-card settlements (when the card is connected) are excluded so nothing is counted
    twice. kind='distribution' lists dividends and withholding tax paid by the company; kind='transfer' lists the
    moves themselves (own accounts, household transfers, investments, company current account; out = positive)."""
    today = _today()
    with _conn() as c:
        return _out(A.breakdown(c, _date(date_from, today.replace(day=1)), _date(date_to, today), group_by, kind,
                                A.resolve_accounts(c, account), perspective=perspective))


@_tool(READ)
def monthly_cashflow(months: int = 12, account: Optional[str] = None, perspective: Perspective = "personal") -> str:
    """Income, spending (business: costs), net and money moved to investments per calendar month (current month is
    partial). The business perspective also shows dividends paid."""
    with _conn() as c:
        return _out(A.cashflow(c, max(1, min(months, 36)), _today(), A.resolve_accounts(c, account),
                               perspective=perspective))


@_tool(READ)
def recurring_payments(include_income: bool = True, include_inactive: bool = False,
                       perspective: Optional[Literal["personal", "business"]] = None) -> str:
    """Detected subscriptions, bills and regular income: cadence, typical amount, last and next expected date,
    payment method (direct debit / standing order / card), monthly equivalent, and the latest price change.
    Use for "what subscriptions do I have?", "what are my fixed costs?", "did anything get more expensive?"."""
    with _conn() as c:
        items = A.recurring(c, _today(), include_income=include_income, include_inactive=include_inactive,
                            perspective=perspective)
    for i in items:
        i.pop("account_id", None)
    out_fixed = sum(i["monthly_equivalent"] for i in items if i["direction"] == "out" and i["kind"] == "expense")
    return _out({"monthly_fixed_costs": A.r2(out_fixed), "items": items})


@_tool(READ)
def upcoming_bills(days: int = 30, include_transfers: bool = False,
                   perspective: Optional[Literal["personal", "business"]] = None) -> str:
    """Bill calendar: expected recurring payments (and income) from today for `days` days, projected from
    payment history, each tagged personal or business. Late items were expected recently but haven't appeared."""
    with _conn() as c:
        return _out(A.upcoming(c, max(1, min(days, 366)), _today(), include_transfers, perspective=perspective))


@_tool(READ)
def unusual_activity(days: int = 60, perspective: Optional[Literal["personal", "business"]] = None) -> str:
    """Things worth a look in the last `days` days: possible double charges, unexpected bank fees or debit
    interest, payments far above the usual amount for that merchant, large payments to new merchants,
    foreign-currency payments, and recurring payments that got more expensive in the last six months."""
    with _conn() as c:
        return _out({"alerts": A.unusual(c, max(7, min(days, 365)), _today(), perspective=perspective)})


@_tool(READ)
def cash_until_payday(payday_day: Optional[int] = None) -> str:
    """"How much can I still spend before payday?" (personal accounts). Uses the available balance of current
    accounts, the next
    payday (detected from salary, or `payday_day` of the month), bills and planned transfers due before then,
    and the usual day-to-day spending pace over the last 90 days."""
    cfg = config.load()
    with _conn() as c:
        return _out(A.until_payday(c, _today(), payday_day or cfg.get("payday_day")))


@_tool(READ)
def list_categories() -> str:
    """The category list with kind (expense / income / transfer) and group, plus how many transactions each has."""
    with _conn() as c:
        counts = dict(c.execute("SELECT category, COUNT(*) FROM transactions GROUP BY category").fetchall())
    return _out([{"category": n, "kind": k, "group": g, "transactions": counts.get(n, 0)}
                 for n, k, g in categorize.CATEGORIES])


@_tool(LOCAL_EDIT)
def recategorize_transaction(transaction_id: str, category: str) -> str:
    """Put one transaction in a different category (local copy only; the bank is never changed). Hand-picked
    categories are never overwritten by rules. Confirm with the user before calling."""
    with _conn() as c:
        return _out(categorize.set_category(c, transaction_id, category))


@_tool(LOCAL_EDIT)
def set_account_scope(account: str, scope: Literal["personal", "business"]) -> str:
    """Mark an account as personal or business (e.g. a company account not detected from its name). Re-runs the
    categorisation. Local only. Confirm with the user before calling."""
    with _conn() as c:
        ids = A.resolve_accounts(c, account)
        if len(ids) != 1:
            return _out({"error": f"'{account}' matches {len(ids)} accounts; be more specific."})
        return _out(categorize.set_scope(c, ids[0], scope))


@_tool(LOCAL_EDIT)
def set_household_member(name: str, names_as_banks_write_them: list[str], remove: bool = False) -> str:
    """Register a household member whose own accounts aren't linked (e.g. a partner), with their full name as it
    appears on statements (order doesn't matter: 'An Peeters' also matches 'Peeters An'). Transfers
    with them become 'Household transfers'. Re-runs the categorisation. Local only. Confirm with the user first."""
    cfg = config.load()
    members = dict(cfg.get("household_members") or {})
    if remove:
        members.pop(name, None)
    else:
        members[name] = [n for n in names_as_banks_write_them if n.strip()]
    cfg["household_members"] = members
    config.save(cfg)
    with _conn() as c:
        n = categorize.recategorize(c)
        hits = c.execute("SELECT COUNT(*), ROUND(COALESCE(SUM(amount),0),2) FROM transactions "
                         "WHERE category='Household transfers' AND merchant=?", (name,)).fetchone()
    return _out({"household_members": members, "transactions_rechecked": n,
                 "household_transfers_with_member": {"count": hits[0], "net": hits[1]}})


@_tool(READ)
def unlinked_own_accounts() -> str:
    """Belgian accounts the user moves money to and from as if they were their own (usually savings accounts the
    bank doesn't share through open banking) that aren't linked or added yet, with last 4 digits, holder, likely
    bank and totals. Use it to find savings accounts to add with add_manual_account."""
    from . import manual
    with _conn() as c:
        return _out({"candidates": manual.suggestions(c), "manual_accounts": manual.list_accounts(c)})


@_tool(LOCAL_EDIT)
def add_manual_account(iban_last4: str, balance: float, balance_date: Optional[str] = None,
                       name: Optional[str] = None, bank: Optional[str] = None,
                       scope: Optional[Literal["personal", "business"]] = None,
                       account_type: Literal["savings", "current", "other"] = "savings") -> str:
    """Add an account the bank doesn't share (typically a savings account), identified by the last 4 digits of
    its IBAN as seen in transfers (see unlinked_own_accounts) or the full IBAN the user gives. `balance` is today's
    balance (or on `balance_date`, YYYY-MM-DD) from the user's banking app. From then on centen estimates the
    balance by adding the transfers seen on linked accounts; interest is only included when the user enters a new
    balance. Re-categorises transfers to it as moves between own accounts. Confirm with the user first."""
    from . import manual
    with _conn() as c:
        try:
            res = manual.add_account(c, iban_last4, balance, balance_date, name, bank, scope, account_type)
        except ValueError as e:
            return _out({"error": str(e)})
        from . import dashboard
        dashboard.refresh(c)
        return _out(res)


@_tool(LOCAL_EDIT)
def set_manual_balance(account: str, balance: float, balance_date: Optional[str] = None) -> str:
    """Update the balance of a manually added account (name, bank or last 4 digits) with the figure from the
    user's banking app, e.g. after interest was paid. Confirm the amount with the user first."""
    from . import manual
    with _conn() as c:
        try:
            res = manual.set_balance(c, account, balance, balance_date)
        except ValueError as e:
            return _out({"error": str(e)})
        from . import dashboard
        dashboard.refresh(c)
        return _out(res)


# --- investments, pension, property and loans --------------------------------------------

AssetType = Literal["investment", "crypto", "pension", "insurance", "property", "asset", "loan"]


class Position(TypedDict, total=False):
    name: str
    isin: str
    symbol: str
    asset_class: str  # etf | equity | fund | bond | crypto | cash | other
    quantity: float
    price: float
    currency: str
    value: float  # in EUR
    cost: float   # what was paid, in EUR (optional)


def _asset_call(fn, *args, **kwargs) -> str:
    from . import assets, dashboard
    with _conn() as c:
        try:
            res = getattr(assets, fn)(c, *args, **kwargs, today=_today())
        except ValueError as e:
            return _out({"error": str(e)})
        dashboard.refresh(c)
        return _out(res)


@_tool(READ)
def list_assets() -> str:
    """Investment, crypto, pension, insurance, property and loan accounts the user added (open banking doesn't cover
    them): value with the date it was entered (estimated = that value plus money moved in or out since; stale = old),
    contributions (net money put in), gain, positions, what recognises their payments, and totals. Also lists brokers
    seen in the transfers that aren't added yet and insurance premiums that may be a pension contract
    (suggestions: confirm with the user before adding)."""
    from . import assets
    with _conn() as c:
        res = assets.summary(c, _today())
        res["suggestions"] = assets.suggestions(c)
        return _out(res)


@_tool(LOCAL_EDIT)
def add_asset_account(name: str, asset_type: AssetType, value: Optional[float] = None, value_date: Optional[str] = None,
                      provider: Optional[str] = None, scope: Optional[Literal["personal", "business"]] = None,
                      match: Optional[list[str]] = None, counterparty_accounts: Optional[list[str]] = None,
                      contributions: Optional[float] = None, contributions_date: Optional[str] = None) -> str:
    """Add an investment account, crypto wallet, pension or insurance contract, property or loan. asset_type:
    investment (broker, funds), crypto, pension (pensioensparen, VAPZ, IPT, groepsverzekering), insurance (tak 21/23),
    property, asset (anything else), loan (value = amount still owed). value is in EUR on value_date (YYYY-MM-DD,
    default today). Payments are recognised by `counterparty_accounts` (IBANs or last 4 digits seen in transfers)
    and/or `match` (text patterns, e.g. ["scala privilege"]); with neither, a broker of the same name seen in the
    transfers (see list_assets suggestions) is taken over automatically. scope: business when the company owns it.
    A company paying into a personal pension stays a cost for the company. `contributions` = total put in up to
    contributions_date, for money paid before the bank history starts. Confirm figures with the user first."""
    return _asset_call("add_account", name, asset_type, value, value_date, provider, scope, match,
                       counterparty_accounts, contributions, contributions_date)


@_tool(LOCAL_EDIT)
def set_asset_value(account: str, value: float, value_date: Optional[str] = None) -> str:
    """Enter what an investment, pension, property or loan account is worth today (or on value_date), from the
    broker app, insurer statement or loan table. For loans: the amount still owed. Confirm the figure first."""
    return _asset_call("set_value", account, value, value_date)


@_tool(LOCAL_EDIT)
def update_asset_account(account: str, name: Optional[str] = None, provider: Optional[str] = None,
                         scope: Optional[Literal["personal", "business"]] = None, asset_type: Optional[AssetType] = None,
                         add_match: Optional[list[str]] = None, remove_match: Optional[list[str]] = None,
                         add_counterparty_accounts: Optional[list[str]] = None,
                         remove_counterparty_accounts: Optional[list[str]] = None,
                         contributions: Optional[float] = None, contributions_date: Optional[str] = None) -> str:
    """Change an asset account: rename, type, scope, how its payments are recognised (text patterns or
    counterparty accounts), or the total contributions up to a date (e.g. 'I put 60.000 into DEGIRO before 2025').
    Re-runs the categorisation. Confirm with the user first."""
    return _asset_call("update_account", account, name, provider, scope, asset_type, add_match, remove_match,
                       add_counterparty_accounts, remove_counterparty_accounts, contributions, contributions_date)


@_tool(LOCAL_EDIT)
def set_positions(account: str, positions: list[Position], as_of: Optional[str] = None,
                  update_value: bool = True) -> str:
    """Store the holdings of an investment, crypto or pension account on a date (default today), e.g. read from a
    screenshot or statement the user shares. value per position in EUR (or quantity × price for EUR prices). Their
    total becomes the account value unless update_value=False. Replaces earlier positions of the same date.
    Show the user what you read and confirm before calling."""
    return _asset_call("set_positions", account, [dict(p) for p in positions], as_of, update_value)


@_tool(LOCAL_EDIT)
def import_positions(account: str, file_path: str, as_of: Optional[str] = None, update_value: bool = True) -> str:
    """Read only the holdings from a positions export (DEGIRO Portfolio.csv, a LYNX / IBKR statement, or a simple
    CSV table with name, ISIN, quantity, price and value); their total becomes the account value on as_of (default:
    the statement date or today). For full portfolio management prefer import_broker_export."""
    return _asset_call("import_positions", account, file_path, as_of, update_value)


def _portfolio_call(fn, *args, **kwargs) -> str:
    from . import dashboard, portfolio as P
    with _conn() as c:
        try:
            res = getattr(P, fn)(c, *args, **kwargs)
        except ValueError as e:
            return _out({"error": str(e)})
        if fn == "import_files":
            dashboard.refresh(c)
        return _out(res)


@_tool(LOCAL_EDIT)
def import_broker_export(account: str, paths: list[str], force: bool = False) -> str:
    """Import broker exports from this computer into an investment or crypto account (see list_assets), for
    portfolio management. `paths`: CSV files and/or folders (every CSV in a folder is read). Recognised: DEGIRO
    Transactions.csv (buys/sells with EUR value and costs), Account.csv (dividends, dividend tax, interest, fees,
    stock-exchange tax, deposits/withdrawals) and Portfolio.csv (holdings, sets the value); LYNX / Interactive
    Brokers activity statement CSV or Flex Query CSV; Bitvavo transaction history CSV. Rows already imported are
    skipped, so overlapping exports are fine. A file from another broker than the account is refused unless
    force=True. Returns per file what was read, plus the resulting holdings and warnings (e.g. history incomplete)."""
    return _portfolio_call("import_files", account, paths, force, today=_today())


@_tool(READ)
def portfolio(account: Optional[str] = None, include_closed: bool = False) -> str:
    """Portfolio management view from the imported broker exports, for one account or all together: holdings
    (quantity, average cost GAK, cost, price and its source, value, unrealised result, weight), realised results,
    dividends (net of withholding tax), costs, stock-exchange tax (TOB), deposits and withdrawals per year, totals,
    the money-weighted return (XIRR, when the deposits come from the broker export), allocation by asset class and
    currency, and warnings when the history is incomplete. include_closed adds sold-out positions."""
    return _portfolio_call("summary", account, today=_today(), include_closed=include_closed)


@_tool(READ)
def portfolio_transactions(account: Optional[str] = None, instrument: Optional[str] = None,
                           kind: Optional[Literal["buy", "sell", "dividend", "dividend_tax", "interest", "fee", "tax",
                                                  "deposit", "withdrawal", "transfer_in", "transfer_out", "income",
                                                  "other"]] = None,
                           date_from: Optional[str] = None, date_to: Optional[str] = None, limit: int = 100) -> str:
    """The imported broker transactions (newest first): trades, dividends, taxes, costs, deposits. instrument
    matches ISIN, symbol or part of the name. amount_eur is the cash effect (+ = into the broker account)."""
    return _portfolio_call("transactions", account, instrument, kind, date_from, date_to, limit)


@_tool(LOCAL_DELETE)
def remove_asset_account(account: str) -> str:
    """Delete an asset account with its values, positions and payment recognition (the payments themselves stay;
    they go back to their normal categories). Only after the user asked for it."""
    return _asset_call("remove", account)


@_tool(LOCAL_EDIT)
def add_category_rule(pattern: str, category: str, direction: Literal["any", "debit", "credit"] = "any",
                      merchant: Optional[str] = None) -> str:
    """Create a rule so matching transactions (now and in future) get `category`. `pattern` is matched
    case- and accent-insensitively against counterparty, description and message (regex allowed). `merchant`
    optionally sets a clean display name. Re-categorises existing data. Confirm with the user before calling."""
    with _conn() as c:
        return _out(categorize.add_rule(c, pattern, category, direction, merchant))


_SCHEMA_DOC = """Tables: transactions(id, account_id, booking_date, value_date, amount [negative = out], currency,
status [booked|pending], counterparty_name, counterparty_masked, description, remittance, structured_ref,
payment_type [card|direct_debit|transfer|standing_order|instant|cash|fee|other], mandate_id, creditor_id,
bank_tx_code, mcc, merchant, merchant_key, category, category_source, kind [expense|income|transfer], source);
accounts(id, source, bank, name, iban_masked, currency, account_type [current|savings|card|loan],
scope [personal|business], credit_limit); transactions also has internal [own|cross] and kind can be distribution;
balances(account_id, as_of, balance_type [booked|available|manual|valuation|contributions], amount, currency);
categories(name, kind, grp); category_rules(id, pattern, direction, category, merchant); sync_log(account_id,
finished_at, status, n_new). Investment / pension / loan accounts are accounts with source='asset' and account_type
investment|crypto|pension|insurance|property|asset|loan; transactions.asset_id links a payment to one;
asset_links(account_id, kind [iban|match], iban_masked, pattern); positions(account_id, as_of, name, isin, symbol,
asset_class, quantity, price, currency, value_local, value [EUR], cost); inv_transactions(account_id, date, kind
[buy|sell|dividend|dividend_tax|interest|fee|tax|deposit|withdrawal|transfer_in|transfer_out|income|other], name,
isin, symbol, quantity, price, currency, amount, amount_eur [+ = into the broker account], fee_eur, source)."""


def query_sql(sql: str, limit: int = 200) -> str:
    """Run one read-only SQLite SELECT for custom analysis (e.g. average weekly groceries, spending per weekday,
    year-over-year energy costs). Sensitive columns (full IBANs, raw bank payloads) read as NULL and writes are
    rejected. """
    limit = max(1, min(limit, 2000))
    deadline = time.monotonic() + 5
    with closing(db.connect_readonly()) as c:
        c.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 20000)
        try:
            cur = c.execute(sql)
        except Exception as e:  # sqlite3.DatabaseError incl. "not authorized"
            return _out({"error": str(e), "hint": "SELECT only. " + _SCHEMA_DOC})
        cols = [d[0] for d in cur.description or []]
        rows = cur.fetchmany(limit + 1)
    clean = [[mask_ibans_in_text(v) if isinstance(v, str) else v for v in r] for r in rows[:limit]]
    return _out({"columns": cols, "rows": clean, "truncated": len(rows) > limit})


query_sql.__doc__ = (query_sql.__doc__ or "") + _SCHEMA_DOC
_tool(READ)(query_sql)


@_tool(BANK_READ)
def sync_bank_data(force: bool = False) -> str:
    """Fetch new balances and transactions from the linked banks (read-only PSD2 access). Banks may limit
    unattended access to about 4 times a day, so syncs within `min_sync_hours` are skipped unless force=True."""
    cfg = config.load()
    if not cfg["enablebanking"].get("app_id"):
        return _out({"error": "No bank connection configured. In a terminal run `centen init` and "
                              "`centen link --bank <your bank>` (see README), or import CSV files with `centen import`."})
    from . import enablebanking
    with _conn() as c:
        try:
            res = enablebanking.sync(c, cfg, force=force, today=_today())
        except Exception as e:
            return _out({"error": str(e)})
        from . import dashboard
        _quiet_prices(c)
        p = dashboard.refresh(c)
        if p and isinstance(res, dict):
            res["dashboard"] = str(p)
        return _out(res)


def _quiet_prices(c, max_age: int = 15) -> dict | None:
    """Refresh prices if older than max_age minutes; a price problem never blocks anything else. Once a day the
    price history (31/12/2025 and month-ends, for the tax view and net worth history) is completed too."""
    from . import history, prices
    try:
        res = prices.refresh(c, today=_today(), max_age_minutes=max_age)
    except Exception as e:  # noqa: BLE001
        res = {"error": f"prices not refreshed: {e}"}
    try:
        history.ensure_history(c, today=_today())
    except Exception:  # noqa: BLE001
        pass
    return res


@_tool(BANK_READ)
def refresh_prices(force: bool = True) -> str:
    """Fetch current prices for every holding: stocks, ETFs and funds by ISIN from Yahoo Finance (the euro listing
    when there is one, else converted at the current exchange rate) and crypto from Bitvavo's public prices. Only
    instrument codes are sent. Updates each account's value when nearly everything in it has a live price, and the
    dashboard. Prices are also refreshed automatically (when older than 15 minutes) on sync and build_dashboard."""
    from . import dashboard, prices
    with _conn() as c:
        try:
            res = prices.refresh(c, today=_today(), max_age_minutes=0 if force else 15)
        except Exception as e:  # noqa: BLE001
            return _out({"error": f"Couldn't fetch prices: {e}"})
        dashboard.refresh(c)
        return _out(res)


@_tool(LOCAL_EDIT)
def set_price_symbol(instrument: str, symbol: Optional[str] = None) -> str:
    """Fix which quote is used for an instrument when the automatic choice is wrong or missing: instrument = ISIN (or
    coin), symbol = Yahoo Finance symbol (e.g. 'IUSA.AS', 'VWCE.DE') or a Bitvavo market ('BTC-EUR'). Leave symbol
    empty to go back to automatic. Takes effect at the next refresh_prices."""
    from . import prices
    with _conn() as c:
        return _out(prices.set_symbol(c, instrument, symbol))


@_tool(LOCAL_EDIT)
def build_dashboard(refresh_prices: bool = True) -> str:
    """Rebuild the local HTML dashboard (dashboard.html in the centen data folder) from the current data and return
    its path. Tabs: total, personal, business, household, investments, accounts; the file works offline. Holdings get
    current prices first (when older than 15 minutes; refresh_prices=False skips that). Tell the user to open the
    file in their browser. It is also rebuilt automatically after every sync."""
    from . import dashboard
    with _conn() as c:
        if refresh_prices:
            _quiet_prices(c)
        p = dashboard.build(c, today=_today())
    return _out({"dashboard": str(p), "uri": p.resolve().as_uri(),
                 "note": "Open this file in a browser. Cash-basis figures from bank data; investments show the values "
                         "entered per account (estimated in between), or money deposited where no value is known."})


@_tool(READ)
def data_status() -> str:
    """Where the data comes from and how fresh it is: per account the source, history range and last sync;
    per bank link the consent expiry date (PSD2 consents last up to 180 days, then need re-linking)."""
    today = _today()
    with _conn() as c:
        links = [{"bank": r["aspsp_name"], "status": r["status"], "consent_valid_until": (r["valid_until"] or "")[:10],
                  "days_left": (date.fromisoformat(r["valid_until"][:10]) - today).days if r["valid_until"] else None}
                 for r in c.execute("SELECT aspsp_name, status, valid_until FROM bank_links")]
        errors = [dict(r) for r in c.execute(
            "SELECT a.name AS account, s.finished_at, s.message FROM sync_log s JOIN accounts a ON a.id=s.account_id "
            "WHERE s.status='error' AND s.finished_at >= ? ORDER BY s.id DESC LIMIT 5",
            ((today - timedelta(days=7)).isoformat(),))]
        from . import assets
        values = [{k: a.get(k) for k in ("name", "type", "value_as_of", "days_since_value", "stale")}
                  for a in assets.list_accounts(c, today)]
        return _out({"today": today.isoformat(), "accounts": A.accounts_overview(c), "bank_links": links,
                     "asset_values": values,
                     "recent_sync_errors": errors, "pending_transactions":
                         c.execute("SELECT COUNT(*) FROM transactions WHERE status='pending'").fetchone()[0]})


# --- r15: tax view, net worth history, labelling "Other", portfolio analysis, digest, bank history ----------------

def _edit_call(fn, refresh: bool = True):
    from . import dashboard
    with _conn() as c:
        try:
            res = fn(c)
        except (ValueError, FileNotFoundError) as e:
            return _out({"error": str(e)})
        if refresh:
            dashboard.refresh(c)
        return _out(res)


@_tool(READ)
def tax_overview(year: Optional[int] = None, include_sales: bool = False) -> str:
    """Belgian tax view on the investments for a calendar year (default this year): the capital gains tax on
    financial assets from 2026 (10 %, FIFO, acquisition value = closing price of 31/12/2025 for older holdings or the
    higher historical cost until 2030 without creating a loss, yearly exemption € 10.000 per taxpayer with the
    carry-over of an unused first € 1.000), realised gains per year, the exemption left, the latent gain at today's
    prices and which units would use up the exemption; dividends with foreign and Belgian withholding tax, the € 833
    exemption for share dividends and an estimate of the RV to declare; stock-exchange tax (TOB); bond funds that may
    fall under the Reynders tax; and a checklist for the tax return (what a foreign broker or crypto means you must
    declare yourself). A calculation aid, not tax advice: say so, and suggest the accountant checks it."""
    from . import tax
    with _conn() as c:
        rep = tax.report(c, _today(), year)
        if not include_sales:
            rep["capital_gains"]["disposals"] = rep["capital_gains"]["disposals"][-25:]
        rep["capital_gains"]["latent"] = rep["capital_gains"]["latent"][:25]
        return _out(rep)


@_tool(LOCAL_EDIT)
def set_reference_price(instrument: str, price_eur: float, price_date: Optional[str] = None) -> str:
    """Enter the 31/12/2025 closing price (EUR per unit) of an instrument by hand, e.g. from the broker's year-end
    statement, when it couldn't be fetched or differs. instrument = ISIN or coin symbol. It wins over fetched prices."""
    from . import history
    d = _date(price_date, history.PHOTO_DATE)
    return _edit_call(lambda c: history.set_reference(c, instrument.strip().upper(), price_eur, d))


@_tool(BANK_READ)
def fetch_price_history(force: bool = False) -> str:
    """Fetch closing prices on 31/12/2025 and month-end prices since your first broker transaction (Yahoo Finance by
    ISIN, Bitvavo for crypto; only instrument codes are sent), for the tax view, the benchmark and the net worth
    history. Runs by itself once a day with the price refresh; force=True fetches again now."""
    from . import history
    with _conn() as c:
        try:
            res = history.ensure_history(c, today=_today(), force=force)
        except Exception as e:  # noqa: BLE001
            return _out({"error": f"Couldn't fetch price history: {e}"})
        from . import dashboard
        dashboard.refresh(c)
        for k in ("photo", "monthly"):
            if isinstance(res.get(k), dict) and "found" in res[k]:
                res[k] = {"found": len(res[k]["found"]), "missing": res[k]["missing"]}
        return _out(res)


@_tool(READ)
def net_worth_history(months: Optional[int] = None) -> str:
    """Net worth per month-end (cash personal / business, investments, property, loans), reconstructed from the bank
    history, the broker exports and month-end prices, plus the live snapshots the dashboard stores every day. Months
    before a bank account's history starts carry its first known balance back (listed in carried_back): mention that
    those months are estimates."""
    from . import networth
    with _conn() as c:
        return _out(networth.timeline(c, months))


@_tool(READ)
def uncategorised_payments(perspective: Literal["personal", "business"] = "personal", limit: int = 30,
                           date_from: Optional[str] = None) -> str:
    """Payments no rule recognised ('Other expenses' / 'Other income'), grouped per merchant, largest first, each with
    count, total, an example and a suggested category when one is likely (card code, words like 'tuinwerken',
    'garage', or 'probably a person'). Use it to clear out 'Other' together with the user, then label_merchants."""
    from . import review
    with _conn() as c:
        q = review.queue(c, perspective, limit, date_from, _today())
        for i in q["items"]:
            i.pop("transaction_ids", None)
        return _out(q)


class MerchantLabel(TypedDict):
    merchant: str
    category: str


@_tool(LOCAL_EDIT)
def label_merchants(labels: list[MerchantLabel], perspective: Optional[Literal["personal", "business"]] = None,
                    dry_run: bool = False) -> str:
    """Label whole merchants at once (merchant as shown by uncategorised_payments, category from list_categories):
    adds a rule matching the counterparty name, so past and future payments get the category; when there's no safe
    pattern the existing payments are labelled one by one. dry_run=True shows what would change (and what else a rule
    would catch) without saving. Confirm the list with the user first; only the local copy changes."""
    from . import review
    if dry_run:
        with _conn() as c:
            out = []
            for item in labels:
                try:
                    out.append(review.label(c, item["merchant"], item["category"], perspective, dry_run=True))
                except ValueError as e:
                    out.append({"merchant": item["merchant"], "error": str(e)})
            return _out({"dry_run": True, "labels": out})
    return _edit_call(lambda c: review.label_many(c, [dict(x) for x in labels], perspective))


@_tool(READ)
def portfolio_analysis(new_money: float = 0.0) -> str:
    """Allocation of all holdings by asset class, region (the fund's focus), currency and account, against the targets
    set with set_allocation_targets, with the buys and sells to get back on target and how to split `new_money`
    (buy-only); the largest positions; a dividend calendar for the next 12 months (gross, from last year's payments
    at today's quantity); and a benchmark comparison: your broker deposits replayed into one index fund (default
    VWCE) at month-end prices, with both XIRRs. Not investment advice."""
    from . import analysis
    with _conn() as c:
        return _out(analysis.report(c, _today(), new_money))


@_tool(LOCAL_EDIT)
def set_allocation_targets(dimension: Literal["class", "region", "currency", "account"],
                           targets: dict[str, float]) -> str:
    """Set target weights for one dimension, replacing earlier ones, e.g. dimension='class', targets={'etf': 80,
    'crypto': 10, 'bond': 10}. Buckets use the names portfolio_analysis shows (classes: etf, equity, fund, bond,
    crypto, cash, other; regions: Wereld, Wereld ex-VS, VS, Europa, België, Opkomende markten, Japan, Azië-Pacific,
    Obligaties, Crypto, Cash, Overig). Shares as 0.8 or 80; they must add up to 100 %. Empty targets removes them."""
    from . import analysis
    return _edit_call(lambda c: analysis.set_targets(c, dimension, targets))


@_tool(LOCAL_EDIT)
def set_instrument_region(instrument: str, region: str) -> str:
    """Fix the region of an instrument (ISIN or coin) when portfolio_analysis guessed it wrong."""
    from . import analysis
    return _edit_call(lambda c: analysis.set_instrument(c, instrument, region=region))


@_tool(READ)
def portfolio_risk() -> str:
    """Return and risk of the investments (investment and crypto accounts, not the pension): the time-weighted return
    (TWR: the investments' own result, independent of when you deposited; comparable with a fund) per account and in
    total, annualised, volatility, maximum drawdown, best/worst month, against the benchmark (correlation, beta);
    the risk of the current mix replayed over the last 36 months of prices, with each holding's share of the risk and
    the correlations between the largest holdings; a look-through of index funds into regions and sectors
    (approximate index weights), funds that overlap, and a diversification score 0–100 with what pulls it down.
    A planning aid, not advice. Use portfolio_analysis for allocation targets, dividends and the money-weighted XIRR."""
    from . import risk
    with _conn() as c:
        res = risk.report(c, _today())
    tw = res.get("time_weighted") or {}
    for k in ("total", "benchmark"):
        if isinstance(tw.get(k), dict):
            tw[k].pop("returns", None)
            tw[k]["months"] = len(tw[k].get("months") or [])
    tw["line"] = (tw.get("line") or [])[-24:]
    return _out(res)


@_tool(LOCAL_EDIT)
def set_instrument_sector(instrument: str, sector: Optional[str] = None) -> str:
    """The sector of a stock (ISIN) for the look-through in portfolio_risk: IT, Financiën, Industrie,
    Gezondheidszorg, Cons. discretionair, Communicatie, Basisconsumptie, Energie, Materialen, Nutsbedrijven, Vastgoed
    (or Obligaties, Grondstoffen, Gemengd for a fund the look-through doesn't know). Empty removes it."""
    from . import risk
    return _edit_call(lambda c: risk.set_sector(c, instrument, sector))


@_tool(LOCAL_EDIT)
def weekly_digest() -> str:
    """This week's digest: spending and income of the last 7 days, the largest payments, unusual activity, bills in
    the next 14 days, the change in net worth, and reminders (expiring bank consents, old investment values, savings
    interest in January, 31/12 values, tax-return season, exemption left, unlabelled spending). Also written to
    digest.html in the data folder."""
    from . import digest
    with _conn() as c:
        d, p = digest.write(c, _today())
        d["file"] = str(p)
        return _out(d)


@_tool(LOCAL_EDIT)
def import_bank_history(paths: list[str], only_before_linked: bool = True) -> str:
    """Import bank exports from this computer (CSV of KBC, Belfius, BNP, ING, ...; CAMT.053 XML; CODA .cod), files or
    a folder, to add history the bank link doesn't reach (e.g. KBC before August 2026). With only_before_linked (the
    default) rows from the period the bank link already covers are left out, so synced data is never changed;
    overlapping files are de-duplicated. Rebuilds the dashboard and the net worth history."""
    from .importers import import_history
    return _edit_call(lambda c: import_history(c, paths, only_before_linked=only_before_linked))


@_tool(LOCAL_EDIT)
def set_loan_terms(account: str, outstanding: float, maturity_date: str, monthly_rate: float,
                   principal: Optional[float] = None, contract_date: Optional[str] = None,
                   statement_date: Optional[str] = None, instalment: Optional[float] = None) -> str:
    """Give a loan account (add it first with add_asset_account, asset_type='loan', match=[dossier number]) its
    repayment plan from the loan statement: amount still owed on statement_date (default today), last instalment
    date (maturity_date), periodic monthly rate as on the statement (0.14 for 0,14 %), and optionally the amount
    borrowed and contract date. centen then keeps the amount owed up to date month by month and splits every
    instalment into interest and capital; the instalment comes from the repayments seen at the bank unless given.
    Confirm the figures with the user first."""
    from . import loans
    return _edit_call(lambda c: loans.set_terms(c, account, outstanding, maturity_date, monthly_rate, principal,
                                                contract_date, statement_date, instalment, _today()))


@_tool(READ)
def property_and_loans(months_of_schedule: int = 12) -> str:
    """The home (or other property) and its loans: value, amount still owed now (from the repayment plan), equity
    (value − loans), loan-to-value, monthly instalments, interest and capital repaid this year, interest still to
    pay, the date you're debt-free, the next instalments split into interest and capital, and the balance per month
    until the end. The property value is the user's own estimate."""
    from . import loans
    with _conn() as c:
        o = loans.overview(c, _today())
        ids = {r[1]: r[0] for r in c.execute("SELECT a.id, a.name FROM accounts a JOIN loan_terms t ON "
                                             "t.account_id=a.id")}
        for x in o["loans"]:
            x["schedule"] = loans.schedule(c, ids[x["account"]], _today(), months_of_schedule)
        o["curve"] = o["curve"][::3]
        return _out(o)


@_tool(LOCAL_EDIT)
def open_update_app() -> str:
    """Open 'centen bijwerken', a small local web app (this computer only) where the user updates the figures no bank
    link or price feed delivers: savings balances, pension / insurance / other values and contributions, the home's
    value, loan statements, missing 31/12/2025 prices, labels for 'Other', allocation targets, new accounts, and file
    imports (broker exports, older bank history). Returns its address and opens it in the browser."""
    from . import app
    try:
        url = app.start(open_browser=True)
    except OSError as e:
        return _out({"error": str(e)})
    return _out({"url": url, "note": "Runs as long as Claude Desktop is open; every change also rebuilds the dashboard."})


# --- r17: fixed costs, cash forecast, pension, scenario planner, phone notifications ---------------------------

FixedGroup = Literal["Wonen & energie", "Lening", "Verzekeringen", "Abonnementen", "Kinderen", "Mobiliteit",
                     "Gezondheid", "Lidmaatschappen", "Belastingen", "Personeel & advies",
                     "Naar jou (bezoldiging, huur)", "Bankkosten", "Sparen & beleggen", "Overige"]
Cadence = Literal["weekly", "every 2 weeks", "monthly", "every 2 months", "quarterly", "every 6 months", "yearly"]


@_tool(READ)
def fixed_costs(perspective: Optional[Literal["personal", "business"]] = None, include_items: bool = True) -> str:
    """Subscriptions and fixed costs, grouped (housing & energy, loan, insurance, subscriptions, children, car,
    memberships, taxes, staff & advisers, money to the owner, bank fees, saving): monthly and yearly totals per
    perspective and group, every item with cadence, typical amount, next date and status (active / stopped /
    cancelled), price changes of the last 12 months with the extra cost per year, payments that seem to have stopped,
    overlapping services (several streaming services or telecom contracts), big quarterly or yearly bills in the next
    4 months, regular income, and the share of regular income that fixed costs take. Use for "what are my fixed
    costs?", "which subscriptions do I have?", "what got more expensive?". To act: set_fixed_cost / add_fixed_cost;
    cancelling itself happens in the banking app (stop the domiciliëring) or at the provider."""
    from . import fixed
    with _conn() as c:
        rep = fixed.report(c, _today(), perspective)
        if not include_items:
            rep.pop("items")
        return _out(rep)


@_tool(LOCAL_EDIT)
def set_fixed_cost(name: str, status: Optional[Literal["confirmed", "ignored", "cancelled", "active"]] = None,
                   new_name: Optional[str] = None, group: Optional[FixedGroup] = None, note: Optional[str] = None,
                   scope: Optional[Literal["personal", "business"]] = None) -> str:
    """Adjust a fixed cost from fixed_costs: ignored = not a fixed cost (it disappears from the list and the forecast),
    cancelled = the user stopped it (no longer counted or expected), confirmed = keep it as detected, active = undo
    an earlier ignored / cancelled; rename it or move it to another group. Only the local copy changes."""
    from . import fixed
    return _edit_call(lambda c: fixed.set_item(c, name, status, new_name, group, note, scope, _today()))


@_tool(LOCAL_EDIT)
def add_fixed_cost(name: str, amount: float, cadence: Cadence, next_date: Optional[str] = None,
                   group: Optional[FixedGroup] = None, scope: Literal["personal", "business"] = "personal",
                   paid_by: Optional[str] = None, note: Optional[str] = None) -> str:
    """Add a fixed cost the bank data can't show (paid with a card of a bank that isn't linked, by a partner, in cash,
    or a yearly bill that hasn't been paid yet in the history). amount = cost per payment (positive), next_date =
    YYYY-MM-DD of the next payment. It counts in fixed_costs and in the cash forecast. To stop it later:
    set_fixed_cost(name, status='cancelled')."""
    from . import fixed
    return _edit_call(lambda c: fixed.add_manual(c, name, amount, cadence, next_date, group, scope, paid_by, note))


@_tool(READ)
def cash_forecast(months: int = 6, perspective: Literal["personal", "business", "total"] = "personal",
                  include_accounts: bool = True) -> str:
    """Expected cash for the coming months (1 to 18): per perspective the balance now, at the end, the lowest point
    and its date; per bank account the same plus its average variable flow; month by month money in and out; the
    expected payments of the next 60 days; warnings when an account may go below zero or the cash drops under the
    buffer (config forecast.buffer, default € 1.000 personal, € 5.000 business). Built from recurring payments, the
    same month last year for VAT / corporate tax / dividends / the yearly tax bill, and the average of the other
    flows over the last 6 months (one-offs ≥ € 5.000 and money moved to investments left out). An estimate: present
    the low points and their dates, not cents."""
    from . import forecast
    with _conn() as c:
        f = forecast.forecast(c, _today(), months)
    p = f["perspectives"].get(perspective)
    out = {"from": f["from"], "to": f["to"], "perspective": perspective,
           "summary": {k: v for k, v in (p or {}).items() if k != "series"} if p else None,
           "series": p["series"] if p else [], "other_perspectives": {k: {kk: vv for kk, vv in v.items() if kk != "series"}
                                                                      for k, v in f["perspectives"].items() if v and k != perspective},
           "monthly": [m for m in f["monthly"] if m["perspective"] == perspective],
           "events_next_60_days": [e for e in f["events_next_60_days"] if perspective == "total" or e["scope"] == perspective],
           "variable_monthly": [v for v in f["variable"] if perspective == "total" or v["scope"] == perspective],
           "one_offs_left_out": f["one_offs_left_out"], "investing_monthly_avg": f["investing_monthly_avg"],
           "warnings": f["warnings"], "assumptions": f["assumptions"]}
    if include_accounts:
        out["accounts"] = [a for a in f["accounts"] if perspective == "total" or a["scope"] == perspective]
    return _out(out)


@_tool(READ)
def pension_overview() -> str:
    """The four pension pillars: 1 legal pension (estimate from mypension.be, entered), 2 supplementary pension via
    work (IPT, groepsverzekering, VAPZ, POZ: the pension accounts), 3 pensioensparen / langetermijnsparen, 4 own savings
    and investments. Per pillar the value now, the yearly premium seen, a projection at the pension date in today's
    euros, after a rough payout tax, and the monthly income that gives; together compared with today's spending
    (replacement ratio, gap). Plus checks: pensioensparen room this year (2026: € 1.050 or € 1.350), stale reserve
    values, the 80 % rule for premiums paid by the company. Missing birth date or legal pension estimate is listed in
    needs: ask the user and save it with set_pension_profile. A planning aid; the pension fiche and mypension.be are
    leading."""
    from . import pension
    with _conn() as c:
        return _out(pension.overview(c, _today()))


class PensionAccountSetting(TypedDict, total=False):
    account: str
    pillar: int
    expected_at_retirement: float
    premium_yearly: float


@_tool(LOCAL_EDIT)
def set_pension_profile(birth_date: Optional[str] = None, retirement_age: Optional[int] = None,
                        stop_work_age: Optional[int] = None,
                        legal_pension_monthly: Optional[float] = None,
                        partner_legal_pension_monthly: Optional[float] = None,
                        return_pension: Optional[float] = None, return_investments: Optional[float] = None,
                        inflation: Optional[float] = None, payout_tax: Optional[float] = None,
                        withdrawal_rate: Optional[float] = None, live_until: Optional[int] = None,
                        accounts: Optional[list[PensionAccountSetting]] = None,
                        spending_monthly: Optional[float] = None, investing_yearly: Optional[float] = None,
                        investing_years: Optional[int] = None, one_off_threshold: Optional[float] = None,
                        reset: Optional[list[Literal["spending_monthly", "investing_yearly", "investing_years", "stop_work_age",
                                                     "one_off_threshold", "legal_pension_monthly"]]] = None) -> str:
    """Save what the pension overview and the planner need: birth_date (YYYY-MM-DD), retirement_age (default 67),
    stop_work_age (stop working and saving earlier: premiums and new investing stop then, and you live from your
    capital until the pension; reset it with reset=["stop_work_age"]),
    legal_pension_monthly (gross per month in today's euros, from mypension.be), assumptions as fractions (0.025 =
    2,5 %): return_pension (pillars 2/3), return_investments (pillar 4), inflation, payout_tax (2nd pillar),
    withdrawal_rate; per pension account: pillar (2/3/4), expected_at_retirement (from the pension fiche) or
    premium_yearly. Spending and investing come from history by default: spending_monthly overrides the spending
    profile (normally the last 12 months without one-offs), investing_yearly overrides the yearly new money into own
    investments (normally the average over investing_years, default 5), one_off_threshold (default 10000) is the size
    from which a single expense counts as one-off. reset puts the listed settings back to automatic. Only what you
    pass changes."""
    from . import pension
    acc = {a["account"]: {k: v for k, v in a.items() if k != "account"} for a in (accounts or [])}
    return _edit_call(lambda c: pension.set_profile(
        c, birth_date=birth_date, retirement_age=retirement_age, stop_work_age=stop_work_age,
        legal_pension_monthly=legal_pension_monthly,
        partner_legal_pension_monthly=partner_legal_pension_monthly, return_pension=return_pension,
        return_investments=return_investments, inflation=inflation, payout_tax=payout_tax,
        withdrawal_rate=withdrawal_rate, live_until=live_until, accounts=acc, spending_monthly=spending_monthly,
        investing_yearly=investing_yearly, investing_years=investing_years, one_off_threshold=one_off_threshold,
        reset=reset))


@_tool(LOCAL_EDIT)
def mark_one_off(transaction_ids: list[str], one_off: Optional[bool] = True) -> str:
    """Leave payments out of the spending profile used by the pension overview and the planner (a car, a renovation,
    a one-time tax): one_off=True. one_off=False counts a payment that was left out automatically after all;
    one_off=None goes back to automatic. Expenses of € 10.000 or more that don't come back yearly are left out
    automatically (one_off_threshold in set_pension_profile). Find the ids with search_transactions or in
    pension_overview → spending_profile.one_offs. Doesn't change the payment itself or its category."""
    from . import pension
    return _edit_call(lambda c: pension.mark_one_off(c, transaction_ids, one_off))


@_tool(READ)
def scenario_plan(stop_work_age: Optional[float] = None, extra_monthly_saving: float = 0.0,
                  spending_change_pct: float = 0.0, expected_return: Optional[float] = None,
                  inflation: Optional[float] = None, pay_off_loans_now: bool = False,
                  monthly_saving: Optional[float] = None, annual_spending: Optional[float] = None,
                  include_company: Optional[bool] = None, withdrawal_rate: Optional[float] = None) -> str:
    """Scenario planner / financial independence: wealth year by year in today's euros from the real starting point
    (cash, investments, company money after an extraction tax, pension capital from the pension date, legal pension,
    loans that end), with the year and age at which capital × withdrawal rate covers spending, capital at the pension
    date and whether it runs out. Change the assumptions to answer "what if": stop_work_age, extra_monthly_saving,
    spending_change_pct (-0.1 = 10 % less), expected_return / inflation (fractions), pay_off_loans_now, or override
    monthly_saving / annual_spending. Always also returns a comparison of standard scenarios. A planning model, not
    advice; mention the assumptions."""
    from . import planner
    with _conn() as c:
        res = planner.plan(c, _today(), stop_work_age=stop_work_age, extra_monthly_saving=extra_monthly_saving,
                           spending_change_pct=spending_change_pct, return_=expected_return, inflation=inflation,
                           pay_off_loans_now=pay_off_loans_now, withdrawal_rate=withdrawal_rate,
                           monthly_saving=monthly_saving, annual_spending=annual_spending,
                           include_company=include_company)
    yrs = res["projection"]["years"]
    res["projection"]["years"] = yrs[::5] + ([yrs[-1]] if (len(yrs) - 1) % 5 else [])
    return _out(res)


@_tool(LOCAL_EDIT)
def phone_notifications(action: Literal["status", "test", "send_now", "preview"] = "status") -> str:
    """Push notifications to the user's phone via ntfy: status (configured or not, detail level), test (send a test
    message), preview (what would be sent now, without sending), send_now (send what's new now; normally the server
    does this every morning). Setting up needs the topic: on the server run
    `centen notify setup --topic <long random name>` and subscribe to that topic in the ntfy app."""
    from . import notify
    with _conn() as c:
        if action == "test":
            return _out(notify.test())
        if action in ("send_now", "preview"):
            return _out(notify.run_alerts(c, _today(), dry_run=action == "preview"))
    s = notify.settings()
    return _out({"configured": notify.configured(), "server": s["ntfy"]["url"], "detail": s["detail"],
                 "opens": s.get("public_url") or None})


# --- r18: salary or dividend, cars, paying off loans, pension fiche ---------------------------------------------

@_tool(READ)
def remuneration_vs_dividend(profit: Optional[float] = None, remuneration: Optional[float] = None,
                             step: float = 5000.0) -> str:
    """Salary or dividend for the company owner: for a yearly company profit before the director's pay (default:
    last 12 months, cash basis: revenue − VAT − costs) and splits from € 0 to all of it as remuneration, what ends up in
    the owner's pocket: remuneration net of social contributions and personal income tax (2026 brackets, municipal
    surcharge, children), corporate tax (20 % on € 100.000 with at least € 50.000 remuneration, else 25 %), and the
    rest as ordinary dividend (30 %), VVPRbis (18 %, if eligible), liquidation reserve (10 % + 9,8 % after 3 years) or
    kept. Returns the current split, the best one and the gain. A calculation aid, not tax advice: mention the
    simplifications (notes) and suggest checking with the accountant. Settings: set_tax_profile."""
    from . import remuneration as R
    with _conn() as c:
        res = R.compare(c, _today(), profit, remuneration, step)
    return _out(res)


@_tool(LOCAL_EDIT)
def set_tax_profile(municipal_rate: Optional[float] = None, children: Optional[int] = None,
                    vvprbis: Optional[bool] = None, social_fee: Optional[float] = None) -> str:
    """Personal tax settings for remuneration_vs_dividend: municipal_rate in % (e.g. 7.5; it's on the tax bill),
    children = dependent children (raises the tax-free amount), vvprbis = the company's shares qualify for VVPRbis
    (cash contribution after July 2013), social_fee = the social insurance fund's management fee (default 3,05 %)."""
    from . import remuneration as R
    return _edit_call(lambda c: R.set_profile(c, municipal_rate, children, vvprbis, social_fee))


@_tool(LOCAL_EDIT)
def add_vehicle(name: str, purchase_price: float, purchase_date: str, yearly_depreciation: float,
                scope: Literal["personal", "business"] = "personal", provider: Optional[str] = None,
                floor_pct: Optional[float] = None) -> str:
    """Add a car (or boat, equipment) to the net worth at a value that falls by yearly_depreciation % per year from
    the purchase price (declining balance; floor_pct = never below this % of the price). A value entered later with
    set_asset_value (a quote, an offer) becomes the new starting point. The purchase payment stays an expense in the
    spending. Ask the user for the percentage: popular sports cars often lose 3-8 % a year, an ordinary car 12-20 %."""
    from . import depreciation
    return _edit_call(lambda c: depreciation.add_vehicle(c, name, purchase_price, purchase_date, yearly_depreciation,
                                                         scope, provider, floor_pct, _today()))


@_tool(LOCAL_EDIT)
def set_depreciation(account: str, yearly_depreciation: Optional[float] = None, purchase_price: Optional[float] = None,
                     purchase_date: Optional[str] = None, floor_pct: Optional[float] = None,
                     remove: bool = False) -> str:
    """Change (or with remove=True stop) the depreciation schedule of an asset added with add_vehicle, or give an
    existing 'other asset' account a schedule (then purchase_price, purchase_date and yearly_depreciation are needed)."""
    from . import depreciation
    if remove:
        return _edit_call(lambda c: depreciation.remove_schedule(c, account))
    return _edit_call(lambda c: depreciation.set_schedule(c, account, purchase_price, purchase_date,
                                                          yearly_depreciation, floor_pct, _today()))


@_tool(READ)
def loan_prepayment(amount: float, loan: Optional[str] = None, expected_return: float = 0.05,
                    tax_on_gains: float = 0.10) -> str:
    """Pay off a loan early or invest the money? For an amount now: the reinvestment fee (3 months' interest), the
    interest saved and the new end date (shorter) or the new instalment (lower), and what you'd have on the loan's
    original end date versus investing it at expected_return (fraction) with tax_on_gains on the gains; plus the
    break-even return. Without loan: the dearest loan first, all loans listed. Mention the notes (tax benefits on
    older loans aren't included)."""
    from . import prepay
    with _conn() as c:
        try:
            return _out(prepay.analyse(c, amount, loan, expected_return, tax_on_gains, _today()))
        except ValueError as e:
            return _out({"error": str(e)})


@_tool(LOCAL_EDIT)
def import_pension_statement(path: str, account: Optional[str] = None, apply: bool = False) -> str:
    """Read a pension fiche (PDF) from an insurer: the reserve (value now) with its date, the expected capital at the
    pension date and the yearly premium. apply=False shows what was found (with the line it came from) so the user
    can check it; apply=True saves the reserve as the account's value and the other two in the pension profile.
    The update app (Pensioen tab) offers the same with an upload."""
    from . import pension
    p = os.path.expanduser(path)
    if not os.path.exists(p):
        return _out({"error": f"File not found: {path}"})
    return _edit_call(lambda c: pension.import_statement(c, p, account, apply, today=_today()), refresh=apply)


@_tool(READ)
def possessions() -> str:
    """Possessions that aren't investments: the home and other 'asset' accounts (cars, watches, art). Per item the
    value now, the purchase price (or first value entered), the change since, depreciation if any, value history,
    share of net worth; the home's equity after its loans; and what a car and the home cost over the last 12 months
    (fuel, maintenance, energy…, without buying or selling the thing itself). Add one with add_asset_account
    (asset_type='asset') or add_vehicle (with depreciation)."""
    from . import possessions as PO
    with _conn() as c:
        res = PO.overview(c, _today())
    for i in res["items"]:
        i["history"] = i["history"][-13:]
    return _out(res)


@_tool(BANK_READ)
def sync_crypto() -> str:
    """Update crypto by itself: new Bitvavo transactions through its API (a key with only the View right, saved in
    the update app or with set_crypto_source) plus a check of the Bitvavo balance against the holdings, and hardware
    wallets (Ledger…) read from the public blockchain: bitcoin from an xpub/ypub/zpub or addresses (mempool.space),
    ether from an address (public Ethereum node). Runs daily on the server by itself. Only public keys and
    addresses are sent; nothing can be moved with them."""
    from . import crypto_sync, dashboard
    with _conn() as c:
        res = crypto_sync.sync(c, today=_today())
        dashboard.refresh(c)
    return _out(res)


@_tool(LOCAL_EDIT)
def set_crypto_source(account: str, coin: Optional[str] = None, xpub: Optional[str] = None,
                      addresses: Optional[list[str]] = None, remove: bool = False) -> str:
    """Set what sync_crypto reads for a hardware wallet account: coin BTC with an extended PUBLIC key (xpub/ypub/zpub,
    Ledger Live → account → Edit account → Advanced) and/or addresses, or coin ETH with addresses (0x…). remove=True
    stops reading that coin. Never accept a recovery phrase or private key: refuse and tell the user not to share it.
    The Bitvavo API key is entered by the user in the update app (Beleggingen & pensioen → Crypto automatisch), not
    here."""
    from . import crypto_sync
    if not coin:
        with _conn() as c:
            return _out(crypto_sync.status(c))
    return _edit_call(lambda c: crypto_sync.set_wallet(c, account, coin, xpub, addresses, remove), refresh=False)


# --- prompts (show up as slash commands in Claude Desktop) ---------------------------

@server.prompt()
def maandoverzicht(month: str = "") -> str:
    """Monthly money review: spending vs last month, top categories, bills, alerts."""
    which = f"the month {month}" if month else "this month so far, compared with last month at the same point"
    return (f"Give me a clear review of {which}: total spending and income, the biggest categories and what changed, "
            "notable merchants, bills still to come, and anything unusual. Use the centen tools, keep it short, and end "
            "with one or two concrete suggestions. Show a small chart of spending by category.")


@server.prompt()
def abonnementen_check() -> str:
    """Audit subscriptions and fixed costs; spot price increases and things to cancel."""
    return ("List all my subscriptions and recurring bills with their monthly and yearly cost, grouped by category. "
            "Flag price increases, overlapping services (e.g. several streaming services) and anything I may have "
            "forgotten. Tell me the total per month and per year, and where in my banking app I can stop a "
            "domiciliëring or card subscription.")


@server.prompt()
def factuurkalender(days: str = "30") -> str:
    """Bill calendar for the coming weeks, with cash left until payday."""
    return (f"Make a calendar of the bills and income expected in the next {days} days and show it as a visual "
            "calendar. Then tell me how much I can still spend per day until my next payday.")


def main():
    server.run()


if __name__ == "__main__":
    main()
