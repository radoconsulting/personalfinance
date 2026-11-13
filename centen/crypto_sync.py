"""Crypto kept up to date by itself (r20): Bitvavo through its API, a hardware wallet (Ledger, Trezor…) through the
public blockchain.

Bitvavo: an API key with only the "View" right (Bitvavo → Settings → API). New transactions from GET /account/history
are added to the Bitvavo account the same way as a CSV export (rows already imported are skipped by their transaction
id and content); GET /balance is used as a check against the holdings worked out from the transactions.

Hardware wallet: no keys, only what is public. Bitcoin from an extended public key (xpub/ypub/zpub: Ledger Live →
account → ⋯ → Edit account → Advanced) or from addresses; the addresses are derived here (BIP32/44/49/84) and their
balances read from mempool.space. Ether from an address, through a public Ethereum node. The holdings are stored as
the account's positions of today and valued at the latest price.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
from datetime import date, datetime, timezone

from . import assets as AS
from . import config, db

MEMPOOL = "https://mempool.space/api"
ETH_RPC = "https://ethereum-rpc.publicnode.com"


def endpoints() -> dict:
    """Where wallet balances are read. Privacy: the BTC server sees every address of your wallet, from your IP, and can
    link them together; the ETH node sees your address. Point these at your own node to avoid that, in config.json:
    "crypto_endpoints": {"btc": "http://<nas>:3006/api" (your own mempool / Esplora / electrs HTTP API),
                         "eth": "http://<nas>:8545" (your own Ethereum node, or any JSON-RPC endpoint)}."""
    e = config.load().get("crypto_endpoints") or {}
    return {"btc": (e.get("btc") or MEMPOOL).rstrip("/"), "eth": e.get("eth") or ETH_RPC}
BITVAVO = "https://api.bitvavo.com"
GAP = 20
META_KEY = "crypto_sync"


def r2(x):
    return None if x is None else round(float(x), 2)


# --- settings ----------------------------------------------------------------------------------------

def settings() -> dict:
    import os
    cfg = config.load()
    bv = dict(cfg.get("bitvavo") or {})
    if os.environ.get("CENTEN_BITVAVO_KEY"):
        bv.update(key=os.environ["CENTEN_BITVAVO_KEY"], secret=os.environ.get("CENTEN_BITVAVO_SECRET", ""))
    return {"bitvavo": bv, "wallets": list(cfg.get("wallets") or [])}


def status(conn) -> dict:
    s = settings()
    last = json.loads(db.get_meta(conn, META_KEY) or "{}")
    return {"bitvavo": {"configured": bool(s["bitvavo"].get("key") and s["bitvavo"].get("secret")),
                        "account": s["bitvavo"].get("account"),
                        "key_hint": (s["bitvavo"].get("key") or "")[:6] + "…" if s["bitvavo"].get("key") else None},
            "wallets": [{"account": w["account"], "coin": w["coin"], "what": _hint(w)} for w in s["wallets"]],
            "last_run": last}


def _hint(w: dict) -> str:
    v = w.get("xpub") or ", ".join(w.get("addresses") or [])
    return v[:8] + "…" + v[-4:] if len(v) > 16 else v


def set_bitvavo(conn, key: str | None, secret: str | None, account: str = "Bitvavo", http=None) -> dict:
    """Save (or with key=None remove) the Bitvavo API key; checked with one call first."""
    cfg = config.load()
    if not key:
        cfg.pop("bitvavo", None)
        config.save(cfg)
        return {"bitvavo": "removed"}
    acc = AS.find(conn, account)
    if acc["account_type"] != "crypto":
        raise ValueError(f"{acc['name']} is not a crypto account.")
    key, secret = key.strip(), (secret or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9]{20,128}", key) or not re.fullmatch(r"[A-Za-z0-9]{20,256}", secret):
        raise ValueError("That doesn't look like a Bitvavo API key and secret.")
    bal = Bitvavo(key, secret, http).get("/v2/balance")  # raises with Bitvavo's message when the key is wrong
    cfg["bitvavo"] = {"key": key, "secret": secret, "account": acc["name"]}
    config.save(cfg)
    return {"bitvavo": "saved", "account": acc["name"], "coins": len(bal)}


def set_wallet(conn, account: str, coin: str, xpub: str | None = None, addresses: list[str] | None = None,
               remove: bool = False) -> dict:
    acc = AS.find(conn, account)
    if acc["account_type"] != "crypto":
        raise ValueError(f"{acc['name']} is not a crypto account.")
    coin = coin.upper().strip()
    cfg = config.load()
    ws = [w for w in cfg.get("wallets") or [] if not (w["account"] == acc["name"] and w["coin"] == coin)]
    if not remove:
        addresses = [a.strip() for a in (addresses or []) if a and a.strip()]
        xpub = (xpub or "").strip() or None
        if coin == "BTC":
            if xpub:
                _decode_xkey(xpub)  # raises when it isn't a valid extended public key
            for a in addresses:
                if not re.fullmatch(r"(bc1[02-9ac-hj-np-z]{11,71}|[13][1-9A-HJ-NP-Za-km-z]{25,34})", a):
                    raise ValueError(f"{a} is not a bitcoin address.")
        elif coin == "ETH":
            if xpub:
                raise ValueError("For ether give the address (0x…), not an extended key.")
            for a in addresses:
                if not re.fullmatch(r"0x[0-9a-fA-F]{40}", a):
                    raise ValueError(f"{a} is not an Ethereum address.")
        else:
            raise ValueError("Only BTC (xpub/ypub/zpub or addresses) and ETH (addresses) are read automatically; "
                             "enter other coins with set_positions.")
        if not xpub and not addresses:
            raise ValueError("Give an extended public key or at least one address.")
        ws.append({"account": acc["name"], "coin": coin, **({"xpub": xpub} if xpub else {}),
                   **({"addresses": addresses} if addresses else {})})
    cfg["wallets"] = ws
    config.save(cfg)
    return {"wallets": [{"account": w["account"], "coin": w["coin"], "what": _hint(w)} for w in ws]}


# --- Bitvavo ---------------------------------------------------------------------------------------

class Bitvavo:
    def __init__(self, key: str, secret: str, http=None):
        self.key, self.secret = key, secret
        if http is None:
            import httpx
            http = httpx.Client(timeout=20.0)
        self.http = http

    def sign(self, ts: int, method: str, path: str, body: str = "") -> str:
        return hmac.new(self.secret.encode(), f"{ts}{method}{path}{body}".encode(), hashlib.sha256).hexdigest()

    def get(self, path: str, params: dict | None = None):
        from urllib.parse import urlencode
        full = path + ("?" + urlencode(params) if params else "")
        ts = int(time.time() * 1000)
        h = {"Bitvavo-Access-Key": self.key, "Bitvavo-Access-Timestamp": str(ts),
             "Bitvavo-Access-Signature": self.sign(ts, "GET", full), "Bitvavo-Access-Window": "20000"}
        r = self.http.get(BITVAVO + full, headers=h)
        try:
            data = r.json()
        except ValueError:
            data = None
        if r.status_code != 200:
            msg = (data or {}).get("error") if isinstance(data, dict) else None
            raise ValueError(f"Bitvavo {r.status_code}: {msg or r.text[:120]}")
        return data


def _local(ts: str) -> tuple[str, str]:
    """executedAt (ISO in UTC, or milliseconds) → local date and time (as in the CSV export)."""
    from zoneinfo import ZoneInfo
    if re.fullmatch(r"\d{10,13}", str(ts)):
        d = datetime.fromtimestamp(int(ts) / (1000 if len(str(ts)) > 10 else 1), timezone.utc)
    else:
        d = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
    d = d.astimezone(ZoneInfo("Europe/Brussels"))
    return d.date().isoformat(), d.strftime("%H:%M:%S")


def _f(x):
    try:
        return float(x) if x not in (None, "") else None
    except (TypeError, ValueError):
        return None


def history_to_rows(items: list[dict]) -> list[dict]:
    """/account/history items → rows in the shape of the CSV export, for brokers.bitvavo."""
    out = []
    for it in items:
        typ = (it.get("type") or "").lower()
        if typ in ("withdrawal_cancelled", "internal_transfer", "loan"):
            continue
        sc, sa = (it.get("sentCurrency") or "").upper(), _f(it.get("sentAmount"))
        rc, ra = (it.get("receivedCurrency") or "").upper(), _f(it.get("receivedAmount"))
        pc, pa = (it.get("priceCurrency") or "").upper(), _f(it.get("priceAmount"))
        if typ in ("buy", "sell"):
            coin, qty, paid = (rc, ra, sa) if typ == "buy" else (sc, sa, ra)
            price = paid / qty if paid and qty else (pa if pc == "EUR" else None)
            paid_ccy = sc if typ == "buy" else rc
        elif typ == "withdrawal":
            coin, qty, paid, price, paid_ccy = (sc or rc), (sa if sc else ra), None, (pa if pc == "EUR" else None), "EUR"
        else:  # deposit, staking and other rewards
            coin, qty, paid, price, paid_ccy = (rc or sc), (ra if rc else sa), None, (pa if pc == "EUR" else None), "EUR"
        if not coin or qty is None:
            continue
        d, t = _local(it.get("executedAt") or "")
        out.append({"Date": d, "Time": t, "Type": typ, "Currency": coin, "Amount": qty, "Quote Currency": "EUR",
                    "Quote Price": price, "Received / Paid Currency": paid_ccy, "Received / Paid Amount": paid,
                    "Fee currency": (it.get("feesCurrency") or "EUR").upper(), "Fee amount": _f(it.get("feesAmount")) or 0,
                    "Status": "completed", "Transaction ID": it.get("transactionId") or ""})
    return out


def _csv(rows: list[dict]) -> str:
    import csv
    import io
    cols = ["Date", "Time", "Type", "Currency", "Amount", "Quote Currency", "Quote Price", "Received / Paid Currency",
            "Received / Paid Amount", "Fee currency", "Fee amount", "Status", "Transaction ID"]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(cols)
    for r in rows:
        w.writerow(["" if r.get(c) is None else (f"{r[c]:.10f}".rstrip("0").rstrip(".") if isinstance(r.get(c), float)
                                                 else r[c]) for c in cols])
    return buf.getvalue()


def sync_bitvavo(conn, http=None, client: Bitvavo | None = None) -> dict:
    from . import brokers, portfolio
    s = settings()["bitvavo"]
    if not client and not (s.get("key") and s.get("secret")):
        return {"skipped": "no Bitvavo API key"}
    acc = AS.find(conn, s.get("account") or "Bitvavo")
    client = client or Bitvavo(s["key"], s["secret"], http)
    last = conn.execute("SELECT MAX(date) FROM inv_transactions WHERE account_id=? AND source='bitvavo'",
                        (acc["id"],)).fetchone()[0]
    params = {"maxItems": 100}
    if last:  # a margin of a few days: rows already stored are skipped below
        params["fromDate"] = int(datetime.fromisoformat(last).replace(tzinfo=timezone.utc).timestamp() * 1000) - 4 * 86400000
    items, page = [], 1
    while True:
        res = client.get("/v2/account/history", {**params, "page": page})
        items += res.get("items") or []
        if page >= int(res.get("totalPages") or 1) or page >= 50:
            break
        page += 1
    parsed = brokers.bitvavo(_csv(history_to_rows(items))) if items else {"transactions": [], "warnings": []}
    known_ids = {r[0] for r in conn.execute("SELECT order_id FROM inv_transactions WHERE account_id=? AND "
                                            "order_id IS NOT NULL", (acc["id"],))}

    def ckey(t):  # without the time: the CSV export and the API may write it in another time zone
        return (t["date"], t["kind"], (t["symbol"] or "").upper(), round(float(t["quantity"] or 0), 6),
                round(float(t["amount"] or 0), 2))
    stored = {}
    for e in conn.execute("SELECT date, kind, symbol, quantity, amount FROM inv_transactions WHERE account_id=? AND "
                          "source='bitvavo'", (acc["id"],)):
        k = ckey({"date": e[0], "kind": e[1], "symbol": e[2], "quantity": e[3], "amount": e[4]})
        stored[k] = stored.get(k, 0) + 1
    rows, seen = [], {}
    for t in parsed["transactions"]:
        if t.get("order_id") and t["order_id"] in known_ids:
            continue
        k = ckey(t)
        seen[k] = seen.get(k, 0) + 1
        if seen[k] <= stored.get(k, 0):
            continue
        t = dict(t, account_id=acc["id"], source="bitvavo", file="bitvavo-api",
                 id=brokers.make_id(acc["id"], "bitvavo", "api|" + (t.get("order_id") or t["ext"])))
        rows.append([t.get(c) for c in portfolio.INV_COLS])
    before = conn.total_changes
    conn.executemany(f"INSERT OR IGNORE INTO inv_transactions({','.join(portfolio.INV_COLS)}) "
                     f"VALUES ({','.join('?' * len(portfolio.INV_COLS))})", rows)
    new = conn.total_changes - before
    conn.commit()
    # check: the balance at Bitvavo against the holdings worked out from the transactions
    check = []
    try:
        bal = {b["symbol"].upper(): (_f(b.get("available")) or 0) + (_f(b.get("inOrder")) or 0)
               for b in client.get("/v2/balance") or []}
        try:
            for b in client.get("/v2/stakingBalance") or []:
                sym = (b.get("symbol") or "").upper()
                amt = next((_f(b.get(k)) for k in ("amount", "balance", "staked", "available") if _f(b.get(k)) is not None), 0)
                if sym:
                    bal[sym] = bal.get(sym, 0) + (amt or 0)
        except ValueError:
            pass
        held = {(p["symbol"] or p["name"] or "").upper(): p["quantity"]
                for p in portfolio.compute(conn, acc["id"])["positions"].values() if p["quantity"] > 1e-9}
        for sym in sorted(set(bal) | set(held)):
            if sym == "EUR":
                continue
            a, b = bal.get(sym, 0.0), held.get(sym, 0.0)
            if abs(a - b) > max(1e-6, 0.005 * max(a, b)):
                check.append({"coin": sym, "at_bitvavo": round(a, 8), "from_transactions": round(b, 8)})
    except ValueError as e:
        check = [{"error": str(e)}]
    return {"account": acc["name"], "fetched": len(items), "new": new, "differences": check}


# --- hardware wallet: bitcoin ----------------------------------------------------------------------

P = 2 ** 256 - 2 ** 32 - 977
N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
G = (0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798,
     0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8)


def _add(a, b):
    if a is None:
        return b
    if b is None:
        return a
    if a[0] == b[0] and (a[1] + b[1]) % P == 0:
        return None
    if a == b:
        m = 3 * a[0] * a[0] * pow(2 * a[1], -1, P) % P
    else:
        m = (b[1] - a[1]) * pow(b[0] - a[0], -1, P) % P
    x = (m * m - a[0] - b[0]) % P
    return x, (m * (a[0] - x) - a[1]) % P


def _mul(k, pt=G):
    out = None
    while k:
        if k & 1:
            out = _add(out, pt)
        pt = _add(pt, pt)
        k >>= 1
    return out


def _ser(pt) -> bytes:
    return bytes([2 + (pt[1] & 1)]) + pt[0].to_bytes(32, "big")


def _deser(b: bytes):
    x = int.from_bytes(b[1:], "big")
    y = pow((pow(x, 3, P) + 7) % P, (P + 1) // 4, P)
    if y & 1 != b[0] & 1:
        y = P - y
    return x, y


B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _b58dec(s: str) -> bytes:
    n = 0
    for ch in s:
        n = n * 58 + B58.index(ch)
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
    raw = b"\0" * (len(s) - len(s.lstrip("1"))) + raw
    body, chk = raw[:-4], raw[-4:]
    if hashlib.sha256(hashlib.sha256(body).digest()).digest()[:4] != chk:
        raise ValueError("checksum")
    return body


def _b58enc(body: bytes) -> str:
    raw = body + hashlib.sha256(hashlib.sha256(body).digest()).digest()[:4]
    n, out = int.from_bytes(raw, "big"), ""
    while n:
        n, r = divmod(n, 58)
        out = B58[r] + out
    return "1" * (len(raw) - len(raw.lstrip(b"\0"))) + out


def _ripemd160(data: bytes) -> bytes:
    try:
        return hashlib.new("ripemd160", data).digest()
    except (ValueError, TypeError):
        return _ripemd160_py(data)


def _ripemd160_py(msg: bytes) -> bytes:
    def rol(x, n):
        return ((x << n) | (x >> (32 - n))) & 0xFFFFFFFF
    f = [lambda x, y, z: x ^ y ^ z, lambda x, y, z: (x & y) | (~x & z), lambda x, y, z: (x | ~y) ^ z,
         lambda x, y, z: (x & z) | (y & ~z), lambda x, y, z: x ^ (y | ~z)]
    K1, K2 = [0, 0x5A827999, 0x6ED9EBA1, 0x8F1BBCDC, 0xA953FD4E], [0x50A28BE6, 0x5C4DD124, 0x6D703EF3, 0x7A6D76E9, 0]
    R1 = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 7, 4, 13, 1, 10, 6, 15, 3, 12, 0, 9, 5, 2, 14, 11, 8,
          3, 10, 14, 4, 9, 15, 8, 1, 2, 7, 0, 6, 13, 11, 5, 12, 1, 9, 11, 10, 0, 8, 12, 4, 13, 3, 7, 15, 14, 5, 6, 2,
          4, 0, 5, 9, 7, 12, 2, 10, 14, 1, 3, 8, 11, 6, 15, 13]
    R2 = [5, 14, 7, 0, 9, 2, 11, 4, 13, 6, 15, 8, 1, 10, 3, 12, 6, 11, 3, 7, 0, 13, 5, 10, 14, 15, 8, 12, 4, 9, 1, 2,
          15, 5, 1, 3, 7, 14, 6, 9, 11, 8, 12, 2, 10, 0, 4, 13, 8, 6, 4, 1, 3, 11, 15, 0, 5, 12, 2, 13, 9, 7, 10, 14,
          12, 15, 10, 4, 1, 5, 8, 7, 6, 2, 13, 14, 0, 3, 9, 11]
    S1 = [11, 14, 15, 12, 5, 8, 7, 9, 11, 13, 14, 15, 6, 7, 9, 8, 7, 6, 8, 13, 11, 9, 7, 15, 7, 12, 15, 9, 11, 7, 13, 12,
          11, 13, 6, 7, 14, 9, 13, 15, 14, 8, 13, 6, 5, 12, 7, 5, 11, 12, 14, 15, 14, 15, 9, 8, 9, 14, 5, 6, 8, 6, 5, 12,
          9, 15, 5, 11, 6, 8, 13, 12, 5, 12, 13, 14, 11, 8, 5, 6]
    S2 = [8, 9, 9, 11, 13, 15, 15, 5, 7, 7, 8, 11, 14, 14, 12, 6, 9, 13, 15, 7, 12, 8, 9, 11, 7, 7, 12, 7, 6, 15, 13, 11,
          9, 7, 15, 11, 8, 6, 6, 14, 12, 13, 5, 14, 13, 13, 7, 5, 15, 5, 8, 11, 14, 14, 6, 14, 6, 9, 12, 9, 12, 5, 15, 8,
          8, 5, 12, 9, 12, 5, 14, 6, 8, 13, 6, 5, 15, 13, 11, 11]
    h = [0x67452301, 0xEFCDAB89, 0x98BADCFE, 0x10325476, 0xC3D2E1F0]
    ml = len(msg) * 8
    msg = msg + b"\x80" + b"\0" * ((55 - len(msg)) % 64) + ml.to_bytes(8, "little")
    for i in range(0, len(msg), 64):
        X = [int.from_bytes(msg[i + 4 * j:i + 4 * j + 4], "little") for j in range(16)]
        al, bl, cl, dl, el = h
        ar, br, cr, dr, er = h
        for j in range(80):
            r = j // 16
            t = (rol((al + f[r](bl, cl, dl) + X[R1[j]] + K1[r]) & 0xFFFFFFFF, S1[j]) + el) & 0xFFFFFFFF
            al, el, dl, cl, bl = el, dl, rol(cl, 10), bl, t
            t = (rol((ar + f[4 - r](br, cr, dr) + X[R2[j]] + K2[r]) & 0xFFFFFFFF, S2[j]) + er) & 0xFFFFFFFF
            ar, er, dr, cr, br = er, dr, rol(cr, 10), br, t
        t = (h[1] + cl + dr) & 0xFFFFFFFF
        h[1] = (h[2] + dl + er) & 0xFFFFFFFF
        h[2] = (h[3] + el + ar) & 0xFFFFFFFF
        h[3] = (h[4] + al + br) & 0xFFFFFFFF
        h[4] = (h[0] + bl + cr) & 0xFFFFFFFF
        h[0] = t
    return b"".join(x.to_bytes(4, "little") for x in h)


def _hash160(b: bytes) -> bytes:
    return _ripemd160(hashlib.sha256(b).digest())


_BECH = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


def _bech32(hrp: str, ver: int, prog: bytes) -> str:
    def polymod(values):
        gen = [0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3]
        chk = 1
        for v in values:
            b = chk >> 25
            chk = (chk & 0x1FFFFFF) << 5 ^ v
            for i in range(5):
                chk ^= gen[i] if (b >> i) & 1 else 0
        return chk
    acc, bits, data = 0, 0, [ver]
    for x in prog:
        acc, bits = (acc << 8) | x, bits + 8
        while bits >= 5:
            bits -= 5
            data.append((acc >> bits) & 31)
    if bits:
        data.append((acc << (5 - bits)) & 31)
    hrpx = [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]
    const = 1 if ver == 0 else 0x2BC830A3
    pm = polymod(hrpx + data + [0] * 6) ^ const
    return hrp + "1" + "".join(_BECH[d] for d in data + [(pm >> 5 * (5 - i)) & 31 for i in range(6)])


_VERSIONS = {"0488b21e": "xpub", "049d7cb2": "ypub", "04b24746": "zpub"}


def _decode_xkey(s: str) -> dict:
    try:
        raw = _b58dec(s.strip())
    except (ValueError, IndexError) as e:
        raise ValueError("Not a valid extended public key (xpub/ypub/zpub).") from e
    if len(raw) != 78 or raw.hex()[:8] not in _VERSIONS:
        raise ValueError("Give the extended PUBLIC key (xpub, ypub or zpub) — never a private key or recovery phrase.")
    return {"kind": _VERSIONS[raw.hex()[:8]], "chain": raw[13:45], "key": raw[45:78]}


def _child(key: bytes, chain: bytes, i: int) -> tuple[bytes, bytes]:
    I = hmac.new(chain, key + i.to_bytes(4, "big"), hashlib.sha512).digest()
    pt = _add(_mul(int.from_bytes(I[:32], "big")), _deser(key))
    return _ser(pt), I[32:]


def _address(pub: bytes, script: str) -> str:
    h = _hash160(pub)
    if script == "p2wpkh":
        return _bech32("bc", 0, h)
    if script == "p2sh-p2wpkh":
        return _b58enc(b"\x05" + _hash160(b"\x00\x14" + h))
    return _b58enc(b"\x00" + h)


def derive(xkey: str, script: str, change: int, start: int, count: int) -> list[str]:
    k = _decode_xkey(xkey)
    ck, cc = _child(k["key"], k["chain"], change)
    return [_address(_child(ck, cc, i)[0], script) for i in range(start, start + count)]


def _scripts(kind: str) -> list[str]:
    # Ledger Live shows an "xpub" also for native segwit accounts: try all, keep the ones with history
    return {"zpub": ["p2wpkh"], "ypub": ["p2sh-p2wpkh"]}.get(kind, ["p2wpkh", "p2sh-p2wpkh", "p2pkh"])


def _btc_address(http, a: str) -> tuple[int, bool]:
    r = http.get(f"{endpoints()['btc']}/address/{a}")
    if r.status_code != 200:
        raise ValueError(f"BTC server {r.status_code}")
    d = r.json()
    cs, ms = d.get("chain_stats") or {}, d.get("mempool_stats") or {}
    sats = (cs.get("funded_txo_sum", 0) - cs.get("spent_txo_sum", 0) + ms.get("funded_txo_sum", 0)
            - ms.get("spent_txo_sum", 0))
    return sats, bool(cs.get("tx_count") or ms.get("tx_count"))


def btc_balance(http, xpub: str | None = None, addresses: list[str] | None = None, pause: float = 0.15) -> dict:
    sats, used = 0, 0
    for a in addresses or []:
        s, _ = _btc_address(http, a)
        sats += s
        used += 1
    if xpub:
        kind = _decode_xkey(xpub)["kind"]
        for script in _scripts(kind):
            for chg in (0, 1):
                i, empty = 0, 0
                while empty < GAP and i < 2000:
                    batch = derive(xpub, script, chg, i, 10)
                    for a in batch:
                        s, has = _btc_address(http, a)
                        sats += s
                        if has:
                            used, empty = used + 1, 0
                        else:
                            empty += 1
                        if pause:
                            time.sleep(pause)
                        if empty >= GAP:
                            break
                    i += 10
    return {"quantity": sats / 1e8, "addresses_used": used}


def eth_balance(http, addresses: list[str]) -> dict:
    wei = 0
    for i, a in enumerate(addresses):
        r = http.post(endpoints()["eth"], json={"jsonrpc": "2.0", "id": i + 1, "method": "eth_getBalance", "params": [a, "latest"]})
        d = r.json() if r.status_code == 200 else {}
        if "result" not in d:
            raise ValueError(f"Ethereum node: {(d.get('error') or {}).get('message') or r.status_code}")
        wei += int(d["result"], 16)
    return {"quantity": wei / 1e18, "addresses_used": len(addresses)}


def sync_wallets(conn, http=None, today: date | None = None, pause: float = 0.15) -> dict:
    from . import prices
    today = today or date.today()
    ws = settings()["wallets"]
    if not ws:
        return {"skipped": "no wallets"}
    if http is None:
        import httpx
        http = httpx.Client(timeout=20.0, headers={"User-Agent": "centen"})
    live = prices.latest(conn)
    out = []
    by_acc: dict[str, list] = {}
    for w in ws:
        try:
            res = btc_balance(http, w.get("xpub"), w.get("addresses"), pause) if w["coin"] == "BTC" else \
                eth_balance(http, w.get("addresses") or [])
        except Exception as e:  # noqa: BLE001
            out.append({"account": w["account"], "coin": w["coin"], "error": str(e)[:160]})
            by_acc.setdefault(w["account"], []).append(None)  # leave this account as it is
            continue
        lp = live.get(w["coin"])
        price = lp["price_eur"] if lp else None
        by_acc.setdefault(w["account"], []).append({
            "name": w["coin"], "symbol": w["coin"], "quantity": round(res["quantity"], 8), "price": price,
            "currency": "EUR", "value": r2(res["quantity"] * price) if price is not None else None,
            "asset_class": "crypto"})
        out.append({"account": w["account"], "coin": w["coin"], "quantity": round(res["quantity"], 8),
                    "value": r2(res["quantity"] * price) if price is not None else None,
                    "addresses_used": res["addresses_used"]})
    for name, rows in by_acc.items():
        if any(r is None for r in rows):
            continue  # one coin failed: keep the previous positions rather than store half of them
        acc = AS.find(conn, name)
        snap = conn.execute("SELECT MAX(as_of) FROM positions WHERE account_id=?", (acc["id"],)).fetchone()[0]
        coins = {r["symbol"] for r in rows}
        if snap:  # coins in the wallet that aren't read automatically stay at their last quantity
            for p in conn.execute("SELECT * FROM positions WHERE account_id=? AND as_of=?", (acc["id"], snap)):
                sym = (p["symbol"] or p["name"] or "").upper()
                if sym and sym not in coins:
                    lp = live.get(sym)
                    rows.append({"name": p["name"], "symbol": p["symbol"], "isin": p["isin"], "quantity": p["quantity"],
                                 "price": lp["price_eur"] if lp else p["price"], "currency": "EUR",
                                 "value": r2(p["quantity"] * lp["price_eur"]) if lp and p["quantity"] is not None
                                 else p["value"], "asset_class": p["asset_class"]})
        AS.set_positions(conn, acc["name"], rows, today.isoformat(), True, source="wallet", today=today)
    return {"wallets": out}


def sync(conn, http=None, today: date | None = None) -> dict:
    from . import prices
    today = today or date.today()
    out = {}
    try:
        out["bitvavo"] = sync_bitvavo(conn, http)
    except Exception as e:  # noqa: BLE001
        out["bitvavo"] = {"error": str(e)[:200]}
    try:
        out["wallets"] = sync_wallets(conn, http, today)
    except Exception as e:  # noqa: BLE001
        out["wallets"] = {"error": str(e)[:200]}
    try:
        prices.refresh(conn, http, today, max_age_minutes=10)
    except Exception:  # noqa: BLE001
        pass
    out["at"] = datetime.now().isoformat(timespec="minutes")
    db.set_meta(conn, META_KEY, json.dumps(out, default=str))
    conn.commit()
    return out
