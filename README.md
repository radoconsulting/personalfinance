# centen: Personal Finance for Claude, for Belgian bank accounts

Anthropic's **Personal Finance in Claude** (launched 5 November 2026) links bank accounts through Plaid, but it
is US-only. **centen** gives you the same experience for Belgian accounts. It is a small MCP server that runs
on your own computer and gives Claude read-only access to your KBC, BNP Paribas Fortis, Belfius, ING, Argenta,
Crelan and other accounts, through:

- **PSD2 open banking via [Enable Banking](https://enablebanking.com)**, free for private individuals reading
  their own accounts ("restricted production" mode), or
- **file import** of your bank's CSV or CAMT.053 (ISO 20022 XML) exports, with no third party involved.

Runs on your laptop next to Claude Desktop, or on a NAS / home server in Docker (see below), with a local
dashboard, a small web app to update what no bank shares (savings, pension contracts, the house, loans), a
Belgian tax view (capital gains tax from 2026, dividends, TOB) and net worth over time.

Then you just ask Claude: *"Waar is mijn geld deze maand naartoe?"*, *"Welke abonnementen heb ik en wat kost dat
per jaar?"*, *"Hoeveel kan ik nog uitgeven tot mijn loon binnenkomt?"*, *"Maak een dashboard van mijn vaste kosten."*

## What it does (compared with Claude Personal Finance)

| Personal Finance in Claude (US) | centen (BE) |
|---|---|
| Connect bank and card accounts via Plaid | Enable Banking (PSD2, licensed AISP) or CSV / CAMT.053 import |
| Read-only: can't move, manage or invest money | Read-only: only account-information endpoints, no payment code at all |
| Balances, credit limits, up to 2 years of transactions | Same. The first sync asks for 730 days; how much you get depends on your bank |
| Spending by category, upcoming bills, unusual purchases | `spending_breakdown`, `upcoming_bills`, `unusual_activity` |
| Unexpected fees, rising bills, subscriptions | Fees and debit interest, price increases, recurring-payment and subscription detection |
| "How much is left before payday?" | `cash_until_payday` |
| Charts, calendars, dashboards | Claude builds them from the tool results, or from `query_sql` for anything custom |
| No ads or sponsored recommendations | Same: there is no business model at all |

**Belgian specifics:** categories and rules for Belgian merchants and institutions (Colruyt, Delhaize, Engie,
Luminus, Fluvius, De Watergroep, Farys, Proximus, Telenet, mutualiteiten, Groeipakket, FOD Financiën and the
Vlaamse Belastingdienst, NMBS, De Lijn, 4411, Q8, DEGIRO, LYNX, Bitvavo, and more). It also recognises
**domiciliëringen** (SEPA mandate and creditor ID), **gestructureerde mededelingen** (`+++123/4567/89002+++`,
with the check digits validated) and standing orders. It reads Dutch, French and English statement wording.
Transfers between your own accounts, to savings or to a broker, and credit-card settlements are kept out of
your spending, so nothing is counted twice.

## Privacy: read this

- All data stays in `~/.centen/centen.db` on your computer (or in the `data` folder of your own server). Nothing
  is uploaded anywhere by centen itself; prices are fetched by instrument code only.
- When Claude calls a tool, **the result of that call is sent to Claude** as part of your conversation, like
  anything else you share in a chat. Anthropic's own Personal Finance hub keeps those chats out of model
  training and memory. Here the normal rules of your Claude account apply. Check *Settings → Privacy* (model
  training) and memory if that matters to you.
- Tools only ever show the last four characters of an account number (`BE…7034`). Full IBANs, raw bank
  payloads and bank-session IDs can't be read even through `query_sql`. A SQLite authorizer enforces this.
- With Enable Banking, you log in at your own bank (itsme, the bank app or a card reader). Neither centen nor
  Enable Banking sees your credentials. You can revoke access at your bank or with `centen unlink`.
- This is not financial, tax or investment advice, and figures can lag your bank. Check the bank app before
  making decisions that matter.

## Run it on a NAS or home server (Docker)

Instead of your laptop, centen can live on an always-on box (a Synology or QNAP NAS, a mini PC, a Raspberry Pi).
One container then serves the **dashboard**, the **update app** and the **MCP tools over HTTP**, and runs the
schedule itself (bank sync at 07:40 and 18:40, prices every hour, price history daily, a digest on Monday).

1. Make a folder on the NAS, e.g. `/volume1/docker/centen`, and put `compose.yaml` and `.env.example` from this
   repository in it. Copy `.env.example` to `.env` and set a long random `CENTEN_KEY`. The container runs as the
   owner of the `data` folder by itself (set `PUID`/`PGID` only to pick another user).
2. Start it: Synology **Container Manager → Project → Create**, choose the folder (it reads `compose.yaml`);
   or over SSH `docker compose up -d`. Images exist for amd64 and arm64.
3. Open `http://<nas>:8770`, log in with the key. Empty at first: either
   - **move an existing installation**: run `centen backup <folder>` where centen runs now, and put that file as
     `data/centen.db` next to your `config.json` and the Enable Banking `.pem` key; or
   - **start fresh**: `docker exec -it centen centen init --app-id <id> --key /data/<key>.pem`, then
     `docker exec -it centen centen link --bank KBC` (paste the address your bank redirects to).
4. **Connect Claude Desktop** on any computer at home (or on the VPN): install centen there too (`pipx install
   centen` or `pip install .`) and add to `claude_desktop_config.json`:

   ```json
   "mcpServers": {
     "centen": {"command": "centen", "args": ["remote", "http://nas.local:8770/mcp"],
                "env": {"CENTEN_KEY": "<your key>"}}
   }
   ```

   `centen remote` only relays messages; the data and the bank link stay on the NAS.

**Keep it private.** It is meant for your home network or a VPN such as Tailscale or WireGuard; don't forward the
port on your router. If you put it behind a reverse proxy, use HTTPS. Back up the `data` folder (Hyper Backup,
`centen backup`). Update with `docker compose pull && docker compose up -d`.

Without Docker: `pip install .` and `centen web` does the same (`centen web --show-key` prints the key).

## Install

Requires Python 3.10+.

```bash
pipx install ./centen          # or: uv tool install ./centen   or: pip install ./centen
centen --version
```

## Try it with demo data first

```bash
centen demo        # fictional household at ~/.centen/demo.db (KBC current + savings + Visa, 15 months)
```

Then add it to Claude Desktop with `CENTEN_DB` pointing to the demo file (see below).

## Connect Claude Desktop

Open **Settings → Developer → Edit Config** (that opens `claude_desktop_config.json`) and add:

```json
{
  "mcpServers": {
    "centen": {
      "command": "/full/path/to/centen",
      "args": ["serve"]
    }
  }
}
```

Use `which centen` (macOS/Linux) or `where centen` (Windows) for the full path. For the demo, add
`"env": {"CENTEN_DB": "/Users/<you>/.centen/demo.db"}`. Restart Claude Desktop. The three prompts
`maandoverzicht`, `abonnementen_check` and `factuurkalender` show up as slash commands.

For Claude Code: `claude mcp add centen -- centen serve`.

## Option A: link your bank (Enable Banking, PSD2)

1. Sign in at the [Enable Banking Control Panel](https://enablebanking.com/cp/applications) (email link, no password).
2. **Register an application.** Choose environment **Production**, *Generate in the browser*, and set the redirect
   URL to `https://127.0.0.1:8765/callback` (Enable Banking only accepts https). Privacy and terms URLs are required;
   for personal use any URL works. The browser saves the **private key** as `<application-id>.pem`.
3. **Activate it with your own accounts:** click *Activate by linking accounts* and approve at your bank. The app
   is then active in *restricted mode*: it can only read accounts you linked yourself. Enable Banking's terms allow
   this for personal, non-commercial use.
4. Configure centen:
   ```bash
   centen init --app-id <application-id> --key ~/Downloads/<application-id>.pem
   centen banks                 # exact bank names for BE
   centen link --bank "KBC"     # opens your bank; approve; paste the address you land on; it syncs
   ```
   After approving, your browser lands on `https://127.0.0.1:8765/callback?...code=...` and shows a "can't connect"
   page. That's expected: nothing listens there, so the one-time code never leaves your computer. Copy that address
   into the prompt. Repeat `centen link` for each bank. Activation in step 3 doesn't authorise the API by itself,
   so this step is still needed.
5. Later: `centen sync` (or just ask Claude to refresh: it calls `sync_bank_data`).

Things to know:
- **Consent lasts up to 180 days** (some banks allow less). `data_status` shows the expiry date. Run
  `centen link` again when it runs out.
- Banks may limit unattended access to **4 times a day per account**, so centen skips a sync within 6 hours of
  the last one unless you `--force` it.
- Many banks only serve their full history right after you consent, and about 90 days on later syncs. That is
  why the first sync asks for 2 years.
- For automatic syncing, schedule it, e.g. with cron: `15 7,19 * * * /full/path/to/centen sync`.

## Option B: import files from your bank

In your bank's web banking, export the account history as **CSV** or **XML / CAMT.053**, then:

```bash
centen import ~/Downloads/export.csv
centen import ~/Downloads/visa.csv --account "KBC Visa" --type card   # when the file has no account column
```

The CSV importer recognises column headers in Dutch, French and English, skips preamble lines, detects `;`, `,` or
tab delimiters and Belgian number and date formats, and ignores refused payments. It was tested against the
layouts of KBC, BNP Paribas Fortis / Fintro, ING, Belfius and Argenta. Re-importing overlapping exports never
duplicates rows. Rows already synced from Enable Banking for the same account are skipped.

**CODA** (`.cod`, the Belgian coded statement many banks and accountants use) is read too. To add **older history**
to an account that is already linked (for example KBC before the link started), use `--history`: only rows from
before the linked period are added, so synced data is never touched.

```bash
centen import --history "C:\centen\imports\kbc"      # a folder of CSV / CAMT.053 / CODA files
```

## Personal, business and total

Every account has a **scope**. Accounts held by a company (name contains BV, NV, BVBA, SRL, ...) are `business`;
everything else is `personal`. Change it with `centen scope "<account>" business` or ask Claude. Money between
your company and you is recognised from the account numbers and descriptions:

| Description says | Personal side | Business side |
|---|---|---|
| Verloning / loon / bezoldiging | Director remuneration (income) | Director remuneration (cost) |
| (Interim)dividend | Dividends (income) | Dividends (distribution, not a cost) |
| Huur | Rental income | Rent (cost) |
| anything else | Company current account (transfer) | Company current account (transfer) |

Transfers to an account in your own name that isn't linked count as investments when it's a foreign or broker
account (Trade Republic, LYNX, ...), and as a move between own accounts otherwise. Business accounts also get
VAT, corporate tax, withholding tax, payroll and accounting categories, and unknown credits count as revenue.

Most tools take `perspective` = `personal`, `business` or `total`. `financial_position` gives the whole picture:
net worth, cash per scope, investments (value, contributions, gain), loans, the company's cash-basis result,
personal income and spending, and everything that moved from the company to you.

## Investments, pension, property and loans

Open banking only covers payment accounts. Broker accounts (DEGIRO, LYNX, Bolero, funds), crypto (Bitvavo, a
Ledger wallet), pension savings (pensioensparen, VAPZ, IPT / groepsverzekering), tak 21/23 contracts, your home and
loans are added by hand, or by asking Claude ("voeg DEGIRO toe, waarde € 152.340"):

```bash
centen asset suggest                                   # brokers and pension-like premiums seen in your payments
centen asset add DEGIRO investment --value 152.340     # takes over the transfers to DEGIRO seen in the bank data
centen asset add "IPT NN" pension --value 31.200 --match "scala privilege" --scope personal
centen asset add Woonkrediet loan --value 180.000 --date 2026-11-01   # loan: amount still owed
centen asset value DEGIRO 155.010                      # a new value from the broker app
centen asset update DEGIRO --inleg 60.000 --inleg-date 2024-12-31     # money put in before your bank history
centen asset positions DEGIRO Portfolio.csv            # DEGIRO Portfolio.csv or a LYNX/IBKR activity statement CSV
centen asset list
```

- **Value**: what you enter, on a date. Between updates centen estimates investment accounts as that value plus
  what you moved in or out since; price changes only show up with a new value or a positions import. Values older
  than ~6 weeks (investments) or ~13 months (pension, loans) are flagged.
- **Contributions (inleg)**: transfers recognised by the counterparty account or a text pattern (`--account`,
  `--match`). Gain = value − contributions; it is not a tax figure.
- **Categories**: payments to an investment, crypto, pension or insurance account become *Savings & investments*
  (not spending). Premiums your company pays into your personal pension stay a cost for the company and show up as
  "into your pension" under company → you. Loans and property keep their categories.
- **Net worth** = cash + investments + property − loans. Brokers you haven't added yet count at net deposits
  (never below zero); accounts without a value count at their contributions.

## Tools Claude gets

| Tool | For questions like |
|---|---|
| `financial_position` | "Geef me het totaalbeeld: privé, de BV en mijn beleggingen" |
| `overview` | "Hoe sta ik ervoor deze maand?" (personal, business or total) |
| `list_accounts` | balances, available amount, credit limits |
| `search_transactions` | "Wat heb ik in oktober bij Colruyt uitgegeven?" |
| `spending_breakdown` | by category, group, merchant, month, week or account |
| `monthly_cashflow` | income vs spending vs saved, per month |
| `recurring_payments` | subscriptions, fixed costs, price changes |
| `upcoming_bills` | bill calendar for the next N days |
| `unusual_activity` | double charges, fees and debit interest, unusually large or new payments, price increases |
| `cash_until_payday` | safe-to-spend per day until the next salary |
| `query_sql` | anything custom (read-only SELECT) |
| `list_assets`, `add_asset_account`, `set_asset_value`, `update_asset_account` | "Voeg mijn DEGIRO toe", "LYNX staat op € 98.000", "hoeveel rendement heb ik?" |
| `set_positions`, `import_positions`, `remove_asset_account` | holdings from a screenshot or a broker CSV |
| `list_categories`, `recategorize_transaction`, `add_category_rule`, `set_account_scope` | fixing categories and scopes (local only) |
| `sync_bank_data`, `data_status` | refresh and freshness / consent expiry |
| `tax_overview`, `set_reference_price`, `fetch_price_history` | "Hoeveel meerwaardebelasting betaal ik dit jaar?", "wat moet ik aangeven?" |
| `net_worth_history` | "Hoe evolueerde mijn vermogen het voorbije jaar?" |
| `uncategorised_payments`, `label_merchants` | "Help me ‘Overige’ opruimen" |
| `portfolio_analysis`, `set_allocation_targets`, `set_instrument_region` | spreiding vs doel, bijsturen, dividendkalender, benchmark |
| `weekly_digest`, `import_bank_history` | weekoverzicht met herinneringen; oudere bankhistoriek uit bestanden |

## Cijfers bijwerken (local app)

`centen app` (or double-click `centen-bijwerken.cmd`, or ask Claude to "open centen bijwerken") opens a small web app
on this computer only (127.0.0.1, with a random key in the address) to update what no bank link or price feed gives:
savings balances, pension / insurance / other values and contributions, the home's value, loan statements, missing
31/12/2025 prices, labels for "Other", allocation targets, new accounts, and file imports (broker exports, older bank
history). Every change goes into the same database and rebuilds the dashboard.

## r15: tax view, net worth history, clearing out "Other", analysis, automation

- **Fiscaal** (tab and `centen tax`): the capital gains tax on financial assets from 2026 — 10 % on the net gain per
  year, FIFO, acquisition value = closing price of 31/12/2025 for older holdings (or the higher historical cost until
  2030, never to create a loss), € 10.000 exemption with the carry-over of an unused first € 1.000 (max. 5 years,
  € 5.000); realised and latent gains, which units would use up the exemption; dividends with Belgian and foreign
  withholding tax, the € 833 exemption for share dividends, TOB, bond funds (Reynders), and a checklist of what to
  declare yourself. A calculation aid, not tax advice. Prices of 31/12/2025 and month-ends are fetched once a day
  (`centen tax --fetch`; enter one by hand with `--set ISIN=price`).
- **Vermogen over tijd** (Totaal tab, `centen networth`): month-ends reconstructed from the bank history, broker
  exports and month-end prices, plus a daily snapshot from now on. Months before an account's bank history are marked
  as estimates.
- **Nog te labelen** (Privé and BV tabs, `centen label`): unlabelled payments per merchant with a suggested category;
  `centen label "<merchant>" "<category>"` adds a rule and applies it.
- **Analyse** (Beleggingen tab, `centen analysis`): allocation by class, region, listing currency and account against
  your targets (`--target class:etf=80,crypto=10,bond=10`), rebalancing amounts, a dividend calendar and a comparison
  with a benchmark (VWCE by default; set `"benchmark": {"key": "<ISIN>", "name": "…"}` in config.json).
- **Automatisch** (Windows): `centen schedule install` creates Task Scheduler jobs — sync at 07:40 and 18:40, prices
  every hour 08–22h, and a weekly digest on Monday with a notification (`centen digest --notify`). `centen schedule
  remove` deletes them.

## Categories and your own rules

`centen categories` lists them. Fix things from the CLI or by asking Claude:

```bash
centen rules add "basisschool sint-jan" "Childcare & school"
centen rules add "j\. janssens" "Rent" --direction debit --merchant "Huisbaas"
centen rules list
```

Your rules beat the built-in ones, and categories you set by hand are never overwritten.

## Your data, as a data engineer

It's one SQLite file (tables `transactions`, `accounts`, `balances`, `categories`, `category_rules`, `asset_links`,
`positions`). DuckDB reads it
directly (`ATTACH '~/.centen/centen.db' AS centen (TYPE sqlite);`), so you can model it further with dbt-duckdb.
To delete everything, remove `~/.centen` and revoke the consent at your bank.

## Development

```bash
pip install -e ".[dev]" && pytest
```

The tests cover parsing, categorisation, every CSV layout plus CAMT.053, the Enable Banking flow against a fake API
(JWT verification, paging, pending replacement, expiry), the analyses, and the SQL guard. Works with MCP Python SDK
v1 (`FastMCP`) and v2 (`MCPServer`).

Layout: `money.py` (Belgian parsing), `categorize.py` (rules), `importers/` (CSV, CAMT.053), `enablebanking.py`
(PSD2 client and sync), `analytics.py` (the analyses), `server.py` (MCP tools), `cli.py`.

## License

GPL-3.0-or-later, see [LICENSE](LICENSE). Not affiliated with Anthropic, Enable Banking or any bank.
