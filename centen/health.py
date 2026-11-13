"""Is everything up to date? (r22) Bank consents, the last bank sync, prices, crypto and the server's jobs, in one place.

Used by the status line in the nav bar, the "Te doen" list of the update app and the phone notifications.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

from . import db

CONSENT_WARN_DAYS = 14


def _dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        return None
    return d.astimezone().replace(tzinfo=None) if d.tzinfo else d


def links(conn, today: date | None = None) -> list[dict]:
    """Active bank links with days left and the result of the last sync of their accounts."""
    today = today or date.today()
    out = []
    for r in conn.execute("SELECT id, aspsp_name, status, valid_until FROM bank_links "
                          "WHERE status IN ('active', 'expired') ORDER BY aspsp_name"):
        left = (date.fromisoformat(r["valid_until"][:10]) - today).days if r["valid_until"] else None
        accs = [a[0] for a in conn.execute("SELECT id FROM accounts WHERE link_id=?", (r["id"],))]
        if not accs and r["status"] != "active":
            continue
        last_ok = last_err = None
        if accs:
            q = ",".join("?" * len(accs))
            row = conn.execute(f"SELECT MAX(finished_at) FROM sync_log WHERE status='ok' AND account_id IN ({q})",
                               accs).fetchone()
            last_ok = row[0]
            err = conn.execute(f"SELECT finished_at, message FROM sync_log WHERE status='error' AND account_id IN ({q}) "
                               "ORDER BY id DESC LIMIT 1", accs).fetchone()
            if err and (not last_ok or err[0] > last_ok):
                last_err = {"at": err[0], "message": err[1]}
        out.append({"id": r["id"], "bank": r["aspsp_name"], "status": "expired" if (left is not None and left < 0)
                    else r["status"], "valid_until": (r["valid_until"] or "")[:10] or None, "days_left": left,
                    "accounts": len(accs), "last_ok": last_ok, "last_error": last_err})
    return out


def summary(conn, today: date | None = None, now: datetime | None = None) -> dict:
    """Freshness of each feed plus a list of issues (kind, severity, text, tab of the update app to fix it)."""
    from . import jobs
    today = today or date.today()
    now = now or datetime.now()
    issues = []
    ls = links(conn, today)
    for l in ls:
        if l["status"] == "expired":
            issues.append({"kind": "consent", "severity": "critical", "bank": l["bank"], "tab": "banks",
                           "text": f"Bankkoppeling {l['bank']} is verlopen: opnieuw koppelen."})
        elif l["days_left"] is not None and l["days_left"] <= CONSENT_WARN_DAYS:
            issues.append({"kind": "consent", "severity": "warning" if l["days_left"] > 3 else "critical",
                           "bank": l["bank"], "tab": "banks",
                           "text": f"Bankkoppeling {l['bank']} verloopt over {l['days_left']} "
                                   f"{'dag' if l['days_left'] == 1 else 'dagen'}: opnieuw koppelen."})
        if l["last_error"] and l["status"] == "active":
            issues.append({"kind": "sync_error", "severity": "warning", "bank": l["bank"], "tab": "banks",
                           "text": f"Laatste synchronisatie {l['bank']} mislukt: {l['last_error']['message'][:140]}"})
    bank_times = [_dt(l["last_ok"]) for l in ls if l["last_ok"]]
    bank_last = min(bank_times) if bank_times else None  # the stalest linked bank decides
    if ls and bank_last and now - bank_last > timedelta(hours=36):
        issues.append({"kind": "sync_stale", "severity": "warning", "tab": "banks",
                       "text": f"Bankgegevens niet meer bijgewerkt sinds {bank_last:%d/%m %H:%M}."})

    row = conn.execute("SELECT MAX(fetched_at) FROM prices").fetchone()
    prices_last = _dt(row[0]) if row else None

    crypto = {}
    try:
        crypto = json.loads(db.get_meta(conn, "crypto_sync") or "{}")
    except ValueError:
        pass
    crypto_errors = jobs._errors({k: v for k, v in crypto.items() if k != "at"})
    if crypto_errors:
        issues.append({"kind": "crypto_error", "severity": "warning", "tab": "assets",
                       "text": f"Crypto bijwerken mislukt: {crypto_errors[0][:140]}"})

    js = jobs.status(conn)
    for name in ("backup", "digest", "alerts"):
        j = js.get(name)
        if j and not j.get("ok"):
            issues.append({"kind": f"job_{name}", "severity": "warning", "tab": None,
                           "text": f"Taak '{name}' mislukt op {j['finished'][:16].replace('T', ' ')}: {j['message'][:120]}"})

    def feed(last: datetime | None, ok: bool, stale_after: timedelta, configured: bool = True) -> dict:
        if not configured:
            return {"state": "off", "at": None}
        state = "error" if not ok else ("stale" if not last or now - last > stale_after else "ok")
        return {"state": state, "at": last.isoformat(timespec="minutes") if last else None}

    bank_ok = not any(i["kind"] == "sync_error" or (i["kind"] == "consent" and i["severity"] == "critical")
                      for i in issues)
    feeds = {
        "bank": feed(bank_last, bank_ok, timedelta(hours=36), bool(ls)),
        "prices": feed(prices_last, not (js.get("prices") and not js["prices"].get("ok")), timedelta(hours=26),
                       prices_last is not None),
        "crypto": feed(_dt(crypto.get("at")), not crypto_errors, timedelta(hours=36), bool(crypto)),
    }
    order = {"critical": 0, "warning": 1, "info": 2}
    issues.sort(key=lambda i: order[i["severity"]])
    return {"links": ls, "feeds": feeds, "jobs": js, "issues": issues}
