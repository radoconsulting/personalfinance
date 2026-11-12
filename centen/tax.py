"""Belgian tax view on your investments: the capital gains tax on financial assets (from 2026), dividends and
roerende voorheffing, the stock-exchange tax (TOB) and Reynders-tax flags.

Capital gains tax (meerwaardebelasting, law of April 2026, in force from 1 January 2026), as implemented here:
- 10 % on the net capital gain realised by a private person per calendar year: gains minus losses of the same year
  (losses can't be carried to another year). Costs and taxes are not deductible: proceeds are the gross sale amount
  and the acquisition value excludes purchase costs.
- First-in, first-out per instrument (FIFO).
- Assets acquired before 2026: acquisition value = the closing price of 31 December 2025 ('fotomoment'). For sales up
  to 31 December 2030 the real (historical) acquisition value may be used instead when it is higher, but not to
  create a loss (it is then capped at the sale price).
- Yearly exemption per taxpayer: € 10.000 in 2026 (indexed later). The unused part of its first € 1.000 carries over
  to the next year, each carried amount for at most five years, together at most € 5.000; carried amounts are used
  after the yearly exemption, the oldest first.
- Moving crypto to your own wallet (a Ledger) is not a sale; the lots keep their date and value.

This is a calculation aid for your own tax return, not tax advice: check the figures with your accountant, especially
for derivatives, substantial shareholdings, Reynders funds and accounts held with someone else.
"""
from __future__ import annotations

from collections import defaultdict, deque
from datetime import date

from . import history as H
from .categorize import INVESTING_ASSET_TYPES
from .money import normalize_text

RATE = 0.10
PHOTO = H.PHOTO_DATE
START = date(2026, 1, 1)
HISTORICAL_UNTIL = date(2030, 12, 31)
EXEMPTION = {2026: 10000.0}           # later years: indexed; the amount used is shown and can be set in config.json
CARRY_SLICE = 1000.0
CARRY_YEARS = 5
CARRY_MAX = 5000.0
DIVIDEND_EXEMPTION = {2025: 833.0, 2026: 833.0}  # dividends from shares, per taxpayer (FOD Financiën)
RV = 0.30
EPS = 1e-9
FOREIGN_BROKERS = ("degiro", "flatex", "lynx", "interactive", "ibkr", "bitvavo", "trade republic", "saxo", "etoro",
                   "revolut", "scalable", "bux", "trading 212", "coinbase", "kraken", "binance")


def r2(x):
    return None if x is None else round(float(x), 2)


def eur(x) -> str:
    """€ 1.234,56 the Belgian way."""
    s = f"{abs(x):,.2f}".replace(",", " ").replace(".", ",").replace(" ", ".")
    return ("−" if x < 0 else "") + "€ " + s


def _cfg():
    from . import config
    try:
        return config.load().get("tax") or {}
    except Exception:  # noqa: BLE001
        return {}


def exemption_for(year: int) -> tuple[float, bool]:
    """(amount, known) — amounts after 2026 are the 2026 amount until you set the indexed one in config."""
    cfg = {int(k): float(v) for k, v in (_cfg().get("capital_gains_exemption") or {}).items()}
    if year in cfg:
        return cfg[year], True
    if year in EXEMPTION:
        return EXEMPTION[year], True
    return EXEMPTION[max(EXEMPTION)], False


# --- lots ---------------------------------------------------------------------------------------

def _key(t) -> str:
    return (t["isin"] or (t["symbol"] or "").upper() or normalize_text(t["name"] or "")) or "?"


def _rows(conn, acc_id):
    from .portfolio import _dedupe_corporate
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM inv_transactions WHERE account_id=? ORDER BY date, COALESCE(time,''), "
        "CASE kind WHEN 'deposit' THEN 0 WHEN 'buy' THEN 1 ELSE 2 END, rowid", (acc_id,))]
    return _dedupe_corporate(rows)


def _gross(t) -> float | None:
    """Gross EUR value of a trade (before costs)."""
    q, px = t["quantity"] or 0.0, t["price"]
    if px is not None and (t["currency"] or "EUR") == "EUR":
        return q * px
    if px is not None and t["fx"]:
        return q * px / t["fx"]
    if t["amount_eur"] is not None:
        eur = abs(t["amount_eur"])
        fee = t["fee_eur"] or 0.0
        # Bitvavo books amounts net of its fee; DEGIRO/IBKR keep the fee apart
        if t["source"] == "bitvavo":
            eur = eur + fee if t["kind"] == "sell" else eur - fee
        return eur
    return None


def _photo_snapshot(conn, acc_id):
    """Positions export dated around 31/12/2025, if any: key -> (quantity, value per unit)."""
    d = conn.execute("SELECT as_of FROM positions WHERE account_id=? AND as_of BETWEEN '2025-12-24' AND '2026-01-07' "
                     "ORDER BY abs(julianday(as_of) - julianday('2025-12-31')) LIMIT 1", (acc_id,)).fetchone()
    if not d:
        return None, {}
    out = {}
    for r in conn.execute("SELECT * FROM positions WHERE account_id=? AND as_of=?", (acc_id, d[0])):
        k = r["isin"] or (r["symbol"] or "").upper()
        if not k or r["asset_class"] == "cash" or not r["quantity"]:
            continue
        out[k] = (r["quantity"], (r["value"] / r["quantity"]) if r["value"] else None, r["name"])
    return d[0], out


class _Book:
    """FIFO lots per instrument for one account, with an 'away' pool for units moved to your own wallet."""

    def __init__(self):
        self.lots = defaultdict(deque)      # key -> deque of [qty, date, unit_cost|None]
        self.away = defaultdict(deque)
        self.names = {}

    def qty(self, key):
        return sum(l[0] for l in self.lots[key])

    def add(self, key, q, d, unit):
        self.lots[key].append([q, d, unit])

    def take(self, key, q, pool=None):
        pool = self.lots if pool is None else pool
        out, need = [], q
        while need > EPS and pool[key]:
            lot = pool[key][0]
            use = min(lot[0], need)
            out.append([use, lot[1], lot[2]])
            lot[0] -= use
            need -= use
            if lot[0] <= EPS:
                pool[key].popleft()
        return out, need


def _ref(conn, key, crypto, photo_snap, cache):
    if key in cache:
        return cache[key]
    v = None
    if key in photo_snap and photo_snap[key][1]:
        v = {"price_eur": photo_snap[key][1], "source": "positions export", "date": None}
    c = H.close_on(conn, key, PHOTO, max_gap_days=12)
    if c and c["source"] == "manual":
        v = c  # entered by hand wins
    elif v is None and c:
        v = c
    cache[key] = v
    return v


def _acquisition(lot, ref, sale_date, sale_unit=None):
    """(acquisition value per unit, basis label).

    Before 2026: the 31/12/2025 value. Until 2030 the higher historical value may be used instead, but never to
    create or increase a loss: then the acquisition value is capped at the sale price, and never goes below the
    31/12/2025 value."""
    q, d, unit = lot
    if d >= START.isoformat():
        return unit, "aankoop vanaf 2026" if unit is not None else "onbekend"
    rp = ref["price_eur"] if ref else None
    if rp is None:
        return unit, "historisch (fotomoment ontbreekt)" if unit is not None else "onbekend"
    if unit is not None and unit > rp and sale_date <= HISTORICAL_UNTIL:
        if sale_unit is None or sale_unit >= unit:
            return unit, "historisch (hoger dan fotomoment)"
        if sale_unit > rp:
            return sale_unit, "historisch, begrensd tot de verkoopprijs (geen minwaarde)"
        return rp, "fotomoment 31/12/2025"
    return rp, "fotomoment 31/12/2025"


def _account_lots(conn, acc, ref_cache):
    """Walk one account: FIFO lots and taxable disposals from 2026."""
    crypto = acc["account_type"] == "crypto"
    book = _Book()
    disposals, warnings = [], []
    rows = _rows(conn, acc["id"])
    snap_date, snap = _photo_snapshot(conn, acc["id"])
    topped = False

    def top_up():
        # units in the 31/12/2025 export that the imported history doesn't explain: bought before 2026, cost unknown
        for k, (q, unit_val, name) in snap.items():
            diff = q - book.qty(k)
            if diff > 1e-6:
                book.add(k, diff, "2025-12-31", None)
                book.names.setdefault(k, name)
                warnings.append(f"{name}: {diff:g} units in the export of {snap_date} without imported purchases; "
                                "valued at the fotomoment")

    for t in rows:
        if not topped and t["date"] >= START.isoformat():
            top_up()
            topped = True
        k, q = _key(t), t["quantity"] or 0.0
        if q <= EPS or t["kind"] not in ("buy", "sell", "transfer_in", "transfer_out", "income"):
            continue
        if t["name"]:
            book.names.setdefault(k, t["name"])
        if t["kind"] == "buy":
            g = _gross(t)
            book.add(k, q, t["date"], g / q if g is not None else None)
        elif t["kind"] == "income":
            book.add(k, q, t["date"], t["price"] if t["price"] is not None else 0.0)
        elif t["kind"] == "transfer_in":
            if t["price"] is None:
                back, rest = book.take(k, q, book.away)
                for lot in back:
                    book.add(k, *lot)
                if rest > 1e-8:
                    book.add(k, rest, t["date"], None)
            else:
                book.add(k, q, t["date"], t["price"])
        elif t["kind"] == "transfer_out":
            moved, _ = book.take(k, q)
            for lot in moved:
                book.away[k].append(lot)
        elif t["kind"] == "sell":
            used, short = book.take(k, q)
            sale = date.fromisoformat(t["date"])
            if sale < START:
                continue
            gross = _gross(t)
            if short > 1e-6:
                warnings.append(f"{t['date']}: sold {q:g} {book.names.get(k, k)}, {short:g} more than the imported "
                                "purchases; those units are valued at the fotomoment")
                used.append([short, "2025-12-31", None])
            ref = _ref(conn, k, crypto, snap, ref_cache)
            per_unit = (gross / q) if gross is not None else None
            for lot in used:
                acq, basis = _acquisition(lot, ref, sale, per_unit)
                proceeds = per_unit * lot[0] if per_unit is not None else None
                gain = proceeds - acq * lot[0] if proceeds is not None and acq is not None else None
                if gain is None:
                    warnings.append(f"{t['date']}: no acquisition value for {book.names.get(k, k)}; enter the "
                                    "31/12/2025 price with set_reference_price")
                disposals.append({"date": t["date"], "account": acc["name"], "instrument": book.names.get(k, k),
                                  "isin": t["isin"], "key": k, "quantity": round(lot[0], 8), "proceeds": r2(proceeds),
                                  "acquired": lot[1], "acquisition_per_unit": r2(acq) if acq is not None else None,
                                  "acquisition": r2(acq * lot[0]) if acq is not None else None, "basis": basis,
                                  "gain": r2(gain)})
    if not topped:
        top_up()
    return book, disposals, warnings


def _carry_chain(net_by_year: dict[int, float], upto: int) -> list[dict]:
    out, carried = [], []   # carried: [year_created, amount]
    for y in range(2026, upto + 1):
        carried = [c for c in carried if y - c[0] <= CARRY_YEARS and c[1] > EPS]
        net = max(0.0, net_by_year.get(y, 0.0))
        basic, known = exemption_for(y)
        avail_carry = sum(c[1] for c in carried)
        # the yearly exemption first; carried-over amounts only for what's left (oldest first), so an unused
        # carried amount stays available for its remaining years
        used_basic = min(basic, net)
        rest = net - used_basic
        used_carry = 0.0
        for c in carried:
            u = min(c[1], rest)
            c[1] -= u
            rest -= u
            used_carry += u
        new_carry = max(0.0, CARRY_SLICE - used_basic)
        room = CARRY_MAX - sum(c[1] for c in carried if c[1] > EPS)
        new_carry = max(0.0, min(new_carry, room))
        if new_carry > EPS:
            carried.append([y, new_carry])
        out.append({"year": y, "net_gain": r2(net), "exemption_basic": r2(basic), "exemption_known": known,
                    "exemption_carried_in": r2(avail_carry), "used_carried": r2(used_carry),
                    "used_basic": r2(used_basic), "available": r2(basic + avail_carry),
                    "remaining": r2(basic + avail_carry - used_carry - used_basic), "taxable": r2(rest),
                    "tax_estimate": r2(rest * RATE), "carried_to_next_year": r2(new_carry)})
    return out


def _accounts(conn):
    return [r for r in conn.execute("SELECT * FROM accounts WHERE source='asset' ORDER BY name")
            if r["account_type"] in INVESTING_ASSET_TYPES and r["account_type"] != "pension" and (
                conn.execute("SELECT 1 FROM inv_transactions WHERE account_id=? LIMIT 1", (r["id"],)).fetchone() or
                conn.execute("SELECT 1 FROM positions WHERE account_id=? LIMIT 1", (r["id"],)).fetchone())]


def _withholding(acc) -> dict:
    hay = normalize_text(f"{acc['name']} {acc['bank'] or ''}")
    own = {normalize_text(x) for x in (_cfg().get("withholding_accounts") or [])}
    if normalize_text(acc["name"]) in own:
        return {"withheld_by_broker": True, "note": "je broker houdt de meerwaardebelasting in (volgens je instelling)"}
    if acc["account_type"] == "crypto":
        return {"withheld_by_broker": False, "declare": True,
                "note": "crypto: zelf aangeven in je aangifte (verplicht)"}
    if any(b in hay for b in FOREIGN_BROKERS):
        return {"withheld_by_broker": False, "declare": True,
                "note": "buitenlandse broker: houdt normaal niets in, zelf aangeven"}
    return {"withheld_by_broker": None, "declare": None,
            "note": "controleer of je bank of broker de belasting inhoudt (opt-out kan via je aangifte)"}


def _bond_like(name, asset_class=None):
    n = normalize_text(name or "")
    return asset_class == "bond" or any(w in n for w in ("bond", "obligat", "covered", "treasury", "aggregate",
                                                          "govt", "government", "corp "))


def capital_gains(conn, today: date | None = None, price_lookup: dict | None = None) -> dict:
    """Realised capital gains per year from 2026, the exemption chain, and the latent gain at today's prices."""
    today = today or date.today()
    ref_cache: dict = {}
    disposals, warnings, accounts, latent = [], [], [], []
    away_lots = []
    from . import portfolio as PF
    for acc in _accounts(conn):
        book, disp, warn = _account_lots(conn, acc, ref_cache)
        disposals += disp
        warnings += [f"{acc['name']}: {w}" for w in warn]
        try:
            pf = PF.account_portfolio(conn, acc, today)
            prices = {(h.get("isin") or (h.get("symbol") or "").upper()): h for h in pf["holdings"]}
        except Exception:  # noqa: BLE001
            prices = {}
        if price_lookup:
            prices.update(price_lookup)
        snap_date, snap = _photo_snapshot(conn, acc["id"])
        crypto = acc["account_type"] == "crypto"
        # positions-only accounts (e.g. a Ledger) hold what other accounts moved out
        for pool, where in ((book.lots, acc["name"]), (book.away, f"{acc['name']} → eigen wallet")):
            for k, lots in pool.items():
                lots = [l for l in lots if l[0] > 1e-8]
                if not lots:
                    continue
                h = prices.get(k) or {}
                if pool is book.away and not h:
                    from .prices import latest
                    lp = latest(conn).get(k)
                    h = {"price_eur": lp["price_eur"]} if lp else {}
                qty_h = h.get("quantity") or 0
                unit_now = h.get("price_eur") or ((h["value"] / qty_h) if h.get("value") and qty_h else None)
                ref = _ref(conn, k, crypto, snap, ref_cache)
                q = sum(l[0] for l in lots)
                acq_tot, bases = 0.0, set()
                for l in lots:
                    a, b = _acquisition(l, ref, today, unit_now)
                    bases.add(b)
                    acq_tot += (a or 0.0) * l[0]
                value = unit_now * q if unit_now is not None else None
                latent.append({"account": where, "instrument": book.names.get(k, k), "key": k, "quantity": round(q, 8),
                               "value": r2(value), "acquisition": r2(acq_tot),
                               "gain": r2(value - acq_tot) if value is not None else None,
                               "gain_per_unit": r2((value - acq_tot) / q) if value is not None and q else None,
                               "photo_price": r2(ref["price_eur"]) if ref else None,
                               "photo_source": (ref or {}).get("source"), "basis": sorted(bases),
                               "oldest_lot": min(l[1] for l in lots), "bond_fund": _bond_like(book.names.get(k, k)),
                               "_lots": [[l[0], l[1], l[2]] for l in lots], "_unit_now": unit_now, "_ref": ref})
                if pool is book.away:
                    away_lots.append(k)
        accounts.append({"account": acc["name"], "type": acc["account_type"], **_withholding(acc)})
    disposals.sort(key=lambda d: d["date"])
    by_year = defaultdict(lambda: {"gains": 0.0, "losses": 0.0, "proceeds": 0.0, "sales": 0, "unknown": 0})
    for d in disposals:
        y = by_year[int(d["date"][:4])]
        y["sales"] += 1
        y["proceeds"] += d["proceeds"] or 0
        if d["gain"] is None:
            y["unknown"] += 1
        elif d["gain"] >= 0:
            y["gains"] += d["gain"]
        else:
            y["losses"] += d["gain"]
    net = {y: v["gains"] + v["losses"] for y, v in by_year.items()}
    chain = _carry_chain(net, max(today.year, 2026))
    years = []
    for c in chain:
        v = by_year.get(c["year"], {"gains": 0.0, "losses": 0.0, "proceeds": 0.0, "sales": 0, "unknown": 0})
        years.append({**c, "gains": r2(v["gains"]), "losses": r2(v["losses"]), "proceeds": r2(v["proceeds"]),
                      "sales": v["sales"], "sales_without_value": v["unknown"]})
    cur = next((y for y in years if y["year"] == today.year), None)
    # use the room left this year: which units, oldest first (FIFO), would realise that much gain
    ideas = []
    room = (cur or {}).get("remaining") or 0.0
    if room > 1 and today.year >= 2026:
        for p in sorted([p for p in latent if (p["gain_per_unit"] or 0) > 0 and "eigen wallet" not in p["account"]],
                        key=lambda p: -(p["gain"] or 0))[:6]:
            units, gain, proceeds = 0.0, 0.0, 0.0
            for q, d, unit in p["_lots"]:
                a, _ = _acquisition([q, d, unit], p["_ref"], today, p["_unit_now"])
                g_u = (p["_unit_now"] or 0) - (a or 0)
                if g_u <= 0:
                    continue  # FIFO: an older lot with a loss would be sold first too
                take = min(q, (room - gain) / g_u)
                units += take
                gain += take * g_u
                proceeds += take * (p["_unit_now"] or 0)
                if gain >= room - 0.5:
                    break
            if units > 0:
                ideas.append({"account": p["account"], "instrument": p["instrument"], "units": round(units, 4),
                              "proceeds": r2(proceeds), "gain": r2(gain)})
    for p in latent:
        for k in ("_lots", "_unit_now", "_ref"):
            p.pop(k, None)
    latent.sort(key=lambda p: -(p["value"] or 0))
    missing_photo = sorted({p["instrument"] for p in latent if p["photo_price"] is None and p["oldest_lot"] < "2026"} |
                           {d["instrument"] for d in disposals if d["basis"] in ("onbekend",)})
    lt = [p for p in latent if p["gain"] is not None]
    return {
        "years": years, "current_year": cur, "disposals": disposals, "accounts": accounts,
        "latent": latent,
        "latent_total": {"value": r2(sum(p["value"] or 0 for p in lt)), "acquisition": r2(sum(p["acquisition"] or 0 for p in lt)),
                         "gain": r2(sum(p["gain"] for p in lt)),
                         "tax_if_sold_today": r2(max(0.0, (cur["gains"] + cur["losses"] if cur else 0.0) +
                                                     sum(p["gain"] for p in lt) - (cur["available"] if cur else 0.0))
                                                 * RATE)},
        "use_exemption_ideas": ideas,
        "missing_photo_prices": missing_photo,
        "warnings": list(dict.fromkeys(warnings))[:20],
        "rules": {"rate": RATE, "method": "FIFO", "photo_date": PHOTO.isoformat(),
                  "historical_value_allowed_until": HISTORICAL_UNTIL.isoformat(),
                  "historical_value_no_loss": True,
                  "costs_deductible": False, "losses": "alleen binnen hetzelfde jaar",
                  "exemption_2026": EXEMPTION[2026], "carry": "ongebruikt deel van de eerste € 1.000, max. 5 jaar, "
                                                              "samen max. € 5.000",
                  "per_taxpayer": "Alle rekeningen worden als die van één belastingplichtige geteld. Gehuwd of "
                                  "wettelijk samenwonend: elk heeft een eigen vrijstelling."},
    }


def dividends(conn, today: date | None = None) -> dict:
    """Dividends per year: gross, foreign withholding, Belgian RV status and the € 833 exemption for shares."""
    from . import assets
    today = today or date.today()
    per_year = defaultdict(lambda: {"gross": 0.0, "withheld": 0.0, "be_gross": 0.0, "be_withheld": 0.0,
                                    "foreign_gross": 0.0, "foreign_withheld": 0.0, "shares_gross": 0.0,
                                    "funds_gross": 0.0, "interest": 0.0, "tob": 0.0, "items": defaultdict(float)})
    names = {}
    for acc in _accounts(conn):
        for t in _rows(conn, acc["id"]):
            if t["isin"] and t["name"]:
                names.setdefault(t["isin"], t["name"])
        for t in _rows(conn, acc["id"]):
            if t["amount_eur"] is None:
                continue
            y = per_year[t["date"][:4]]
            isin = t["isin"] or ""
            be = isin.startswith("BE")
            nm = names.get(isin) or t["name"] or isin or "?"
            cls = assets.guess_class(nm, isin, acc["account_type"])
            if t["kind"] == "dividend":
                y["gross"] += t["amount_eur"]
                y["items"][nm] += t["amount_eur"]
                y["be_gross" if be else "foreign_gross"] += t["amount_eur"]
                y["shares_gross" if cls == "equity" else "funds_gross"] += t["amount_eur"]
            elif t["kind"] == "dividend_tax":
                y["withheld"] -= t["amount_eur"]
                y["be_withheld" if be else "foreign_withheld"] -= t["amount_eur"]
            elif t["kind"] == "interest":
                y["interest"] += t["amount_eur"]
            elif t["kind"] == "tax":
                y["tob"] -= t["amount_eur"]
    out = []
    for y in sorted(per_year):
        v = per_year[y]
        ex = DIVIDEND_EXEMPTION.get(int(y), DIVIDEND_EXEMPTION[max(DIVIDEND_EXEMPTION)])
        foreign_net = v["foreign_gross"] - v["foreign_withheld"]
        out.append({"year": int(y), "gross": r2(v["gross"]), "withheld": r2(v["withheld"]),
                    "belgian_gross": r2(v["be_gross"]), "belgian_withheld": r2(v["be_withheld"]),
                    "foreign_gross": r2(v["foreign_gross"]), "foreign_withheld": r2(v["foreign_withheld"]),
                    "foreign_taxable": r2(foreign_net),
                    "rv_due_on_foreign_estimate": r2(max(0.0, foreign_net) * RV),
                    "from_shares": r2(v["shares_gross"]), "from_funds": r2(v["funds_gross"]),
                    "share_dividend_exemption": ex,
                    "exemption_benefit_max": r2(min(ex, v["shares_gross"]) * RV),
                    "interest": r2(v["interest"]), "tob": r2(v["tob"]),
                    "by_instrument": sorted([{"instrument": k, "gross": r2(a)} for k, a in v["items"].items()],
                                            key=lambda x: -x["gross"])[:12]})
    return {"years": out,
            "notes": ["Buitenlandse brokers houden geen Belgische roerende voorheffing in: buitenlandse dividenden "
                      "geef je zelf aan; de schatting is 30 % op bruto min buitenlandse bronheffing.",
                      "De vrijstelling van € 833 geldt voor dividenden van aandelen, niet voor uitkeringen van "
                      "fondsen of ETF's.",
                      "Belgische dividenden: 30 % RV wordt normaal aan de bron ingehouden; terugvragen via code "
                      "1437/2437 binnen de vrijstelling."]}


def reynders(conn, latent: list[dict], disposals: list[dict]) -> list[dict]:
    """Bond funds held or sold: the Reynders tax (30 % on the interest part, needs the fund's TIS) may apply on sale."""
    out = []
    for p in latent:
        if p.get("bond_fund"):
            out.append({"instrument": p["instrument"], "status": "in bezit", "value": p["value"]})
    for d in disposals:
        if _bond_like(d["instrument"]):
            out.append({"instrument": d["instrument"], "status": f"verkocht {d['date']}", "value": d["proceeds"]})
    return out


def report(conn, today: date | None = None, year: int | None = None) -> dict:
    today = today or date.today()
    cg = capital_gains(conn, today)
    dv = dividends(conn, today)
    yr = year or today.year
    checklist = []
    cy = next((y for y in cg["years"] if y["year"] == yr), None)
    if cy and cy["sales"]:
        checklist.append(f"Meerwaarden {yr}: {cy['sales']} verkopen, netto {eur(cy['net_gain'])}; vrijstelling "
                         f"aanvragen in je aangifte (aanslagjaar {yr + 1}).")
    for a in cg["accounts"]:
        if a.get("declare"):
            checklist.append(f"{a['account']}: {a['note']}.")
    dy = next((d for d in dv["years"] if d["year"] == yr), None)
    if dy and dy["foreign_gross"]:
        checklist.append(f"Buitenlandse dividenden {yr}: bruto {eur(dy['foreign_gross'])} aangeven "
                         "(min de eerste schijf van aandelendividenden die vrijgesteld is).")
    if dy and dy["belgian_withheld"]:
        checklist.append(f"Belgische RV {yr}: {eur(dy['belgian_withheld'])} ingehouden; terugvragen binnen de "
                         "vrijstelling via code 1437/2437.")
    if cg["missing_photo_prices"]:
        checklist.append("Fotomoment ontbreekt voor: " + ", ".join(cg["missing_photo_prices"][:6]) +
                         " — haal prijzen op (centen tax --fetch) of geef ze in.")
    rey = reynders(conn, cg["latent"], cg["disposals"])
    if rey:
        checklist.append("Obligatiefondsen (Reyndertaks bij verkoop mogelijk): " +
                         ", ".join(sorted({r["instrument"] for r in rey})[:5]) + ".")
    return {"year": yr, "capital_gains": cg, "dividends": dv, "reynders": rey, "checklist": checklist,
            "disclaimer": "Rekenhulp voor je aangifte, geen fiscaal advies. Laat de cijfers nakijken door je boekhouder."}
