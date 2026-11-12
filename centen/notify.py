"""Push notifications to your phone (r17) through ntfy (https://ntfy.sh, or your own ntfy server on the NAS).

Install the ntfy app, subscribe to a topic with a long random name, and tell centen about it:

    centen notify setup --topic centen-<random> [--url https://ntfy.sh] [--token tk_...] [--detail full]
    centen notify test

What is sent: the weekly digest headline, and once a day (08:30, config schedule.alerts) anything new that needs
attention: a balance that is forecast to drop under your buffer, a possible double payment, an unusually high or a
large new payment, a price increase of a fixed cost, a bank consent that is about to expire. Every alert is sent once.

Privacy: with detail "short" (the default) a notification only says what kind of thing happened ("possible double
payment", "price increase"), without names or amounts, because the message passes through the ntfy server. Use
detail "full" with your own ntfy server, or if that's fine for you. The tap opens your dashboard (config
notify.public_url, e.g. https://centen.<your-nas>.<domain>).
"""
from __future__ import annotations

import hashlib
import json
from datetime import date

from . import analytics as A
from . import config, db

DEFAULT = {"ntfy": {"url": "https://ntfy.sh", "topic": "", "token": ""}, "detail": "short", "public_url": ""}
SENT_KEY = "notified_alerts"

_KIND_NL = {"possible_duplicate": "Mogelijk dubbele betaling", "higher_than_usual": "Hoger bedrag dan gewoonlijk",
            "large_new_merchant": "Grote betaling aan een nieuwe begunstigde", "price_increase": "Prijsstijging",
            "low_balance": "Saldo zakt onder je buffer", "below_zero": "Rekening dreigt onder nul te gaan",
            "consent": "Bankkoppeling verloopt binnenkort", "fee": "Bankkosten of debetrente"}


def settings() -> dict:
    cfg = config.load().get("notify") or {}
    out = json.loads(json.dumps(DEFAULT))
    out.update({k: v for k, v in cfg.items() if k != "ntfy"})
    out["ntfy"].update(cfg.get("ntfy") or {})
    return out


def configured() -> bool:
    return bool(settings()["ntfy"].get("topic"))


def setup(topic: str, url: str | None = None, token: str | None = None, detail: str | None = None,
          public_url: str | None = None) -> dict:
    if not topic or len(topic) < 12:
        raise ValueError("Use a long, hard to guess topic name (12+ characters): anyone who knows it can read it.")
    if detail and detail not in ("short", "full"):
        raise ValueError("detail must be short or full")
    cfg = config.load()
    n = cfg.get("notify") or {}
    n.setdefault("ntfy", {})
    n["ntfy"]["topic"] = topic
    if url:
        n["ntfy"]["url"] = url.rstrip("/")
    if token is not None:
        n["ntfy"]["token"] = token
    if detail:
        n["detail"] = detail
    if public_url is not None:
        n["public_url"] = public_url
    cfg["notify"] = n
    config.save(cfg)
    s = settings()
    return {"topic": s["ntfy"]["topic"], "url": s["ntfy"]["url"], "detail": s["detail"],
            "public_url": s["public_url"] or None, "token": bool(s["ntfy"].get("token"))}


PRIORITY = {"min": 1, "low": 2, "default": 3, "high": 4, "urgent": 5}


def send(title: str, message: str, priority: str = "default", tags: str = "moneybag", click: str | None = None,
         client=None) -> bool:
    """Send one notification (ntfy JSON publish, UTF-8 safe). Never raises: False when not configured or failed."""
    s = settings()
    topic = s["ntfy"].get("topic")
    if not topic:
        return False
    import httpx
    payload = {"topic": topic, "title": title, "message": message, "priority": PRIORITY.get(priority, 3),
               "tags": [t for t in tags.split(",") if t]}
    click = click or s.get("public_url")
    if click:
        payload["click"] = click
    headers = {"Authorization": f"Bearer {s['ntfy']['token']}"} if s["ntfy"].get("token") else {}
    try:
        c = client or httpx.Client(timeout=15)
        r = c.post(s["ntfy"]["url"].rstrip("/") + "/", json=payload, headers=headers)
        return 200 <= r.status_code < 300
    except Exception:  # noqa: BLE001
        return False


def collect(conn, today: date | None = None) -> list[dict]:
    """Things worth a notification today, each with a stable id."""
    today = today or date.today()
    out = []
    for a in A.unusual(conn, 4, today):
        if a["type"] not in ("possible_duplicate", "higher_than_usual", "large_new_merchant", "price_increase", "fee"):
            continue
        if a["type"] != "price_increase" and a["severity"] != "warning" and a["type"] != "large_new_merchant":
            continue
        if a["type"] == "price_increase" and (today - date.fromisoformat(a["date"][:10])).days > 35:
            continue
        out.append({"id": f"{a['type']}|{a.get('transaction_id') or a['merchant']}|{a['date']}", "kind": a["type"],
                    "text": f"{a['merchant']}: {a['detail']}", "amount": a.get("amount"), "scope": a.get("scope")})
    try:
        from . import forecast
        f = forecast.summary(conn, today, 2)
        for w in f["warnings"]:
            kind = "below_zero" if w["severity"] == "critical" else "low_balance"
            out.append({"id": f"{kind}|{w.get('account') or w['scope']}|{w['date'][:7]}", "kind": kind,
                        "text": w["text"], "amount": w.get("amount"), "scope": w["scope"]})
    except Exception:  # noqa: BLE001
        pass
    for r in conn.execute("SELECT aspsp_name, valid_until FROM bank_links"):
        if r["valid_until"]:
            left = (date.fromisoformat(r["valid_until"][:10]) - today).days
            if 0 <= left <= 7:
                out.append({"id": f"consent|{r['aspsp_name']}|{r['valid_until'][:10]}", "kind": "consent",
                            "text": f"{r['aspsp_name']}: toestemming verloopt over {left} dagen.", "amount": None})
    return out


def _sent(conn) -> list[str]:
    try:
        return json.loads(db.get_meta(conn, SENT_KEY) or "[]")
    except ValueError:
        return []


def _h(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()[:16]


def run_alerts(conn, today: date | None = None, client=None, dry_run: bool = False) -> dict:
    """Send what's new since the last run (each alert once)."""
    items = collect(conn, today)
    sent = _sent(conn)
    new = [i for i in items if _h(i["id"]) not in sent]
    if not new:
        return {"new": 0, "sent": False}
    s = settings()
    if s["detail"] == "full":
        body = "\n".join(f"• {i['text']}" for i in new[:8])
    else:
        kinds = sorted({_KIND_NL.get(i["kind"], i["kind"]) for i in new})
        body = "\n".join(f"• {k}" for k in kinds) + "\nOpen centen voor de details."
    title = f"centen: {len(new)} {'melding' if len(new) == 1 else 'meldingen'}"
    prio = "high" if any(i["kind"] in ("below_zero", "possible_duplicate") for i in new) else "default"
    ok = dry_run or send(title, body, prio, "warning" if prio == "high" else "bell", client=client)
    if ok and not dry_run:
        db.set_meta(conn, SENT_KEY, json.dumps((sent + [_h(i["id"]) for i in new])[-400:]))
        conn.commit()
    return {"new": len(new), "sent": ok and not dry_run, "title": title, "body": body}


def send_digest(conn, headline: str, client=None) -> bool:
    s = settings()
    body = headline if s["detail"] == "full" else "Je weekoverzicht staat klaar."
    url = (s.get("public_url") or "").rstrip("/")
    return send("centen weekoverzicht", body, "low", "calendar", click=f"{url}/digest" if url else None, client=client)


def test(client=None) -> dict:
    if not configured():
        return {"sent": False, "error": "Not configured: centen notify setup --topic <long-random-name>"}
    ok = send("centen", "Testmelding: meldingen werken.", "default", "white_check_mark", client=client)
    return {"sent": ok, "topic": settings()["ntfy"]["topic"]}
