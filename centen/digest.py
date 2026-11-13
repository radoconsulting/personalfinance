"""The weekly digest: what happened last week and what needs your attention, as one short page (digest.html in the
data folder) and a one-line Windows notification. Run by the scheduled task `centen digest --notify`, or ask Claude
for `weekly_digest`.

Reminders built in: bank consents about to expire, investment values that are getting old, savings balances to
update after interest (January), 31/12 values for the capital gains tax, the tax-return season, and a large share of
unlabelled spending.
"""
from __future__ import annotations

import html
import json
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

from . import analytics as A


def r2(x):
    return None if x is None else round(float(x), 2)


def _eur(v) -> str:
    if v is None:
        return "—"
    s = f"{abs(v):,.0f}".replace(",", ".")
    return ("−" if v < 0 else "") + "€ " + s


def reminders(conn, today: date) -> list[dict]:
    out = []
    from . import health
    for l in health.links(conn, today):
        left = l["days_left"]
        if left is not None and left <= 30:
            out.append({"kind": "consent", "severity": "warning" if left > 7 else "critical",
                        "text": (f"Toestemming {l['bank']} is verlopen" if left < 0 else
                                 f"Toestemming {l['bank']} verloopt over {left} dagen")
                                + ": opnieuw koppelen in Bijwerken → Banken (of centen link)."})
    try:
        from . import assets
        for a in assets.list_accounts(conn, today):
            if a.get("stale") and a.get("type") not in ("property",):
                out.append({"kind": "stale_value", "severity": "info",
                            "text": f"Waarde {a['name']} is {a.get('days_since_value')} dagen oud: geef een nieuwe "
                                    "waarde of importeer een export."})
    except Exception:  # noqa: BLE001
        pass
    if today.month == 1 or (today.month == 2 and today.day <= 15):
        rows = conn.execute("SELECT a.name, MAX(b.as_of) d FROM accounts a JOIN balances b ON b.account_id=a.id AND "
                            "b.balance_type='manual' WHERE a.source='manual' GROUP BY a.id").fetchall()
        late = [r["name"] for r in rows if r["d"] < f"{today.year}-01-01"]
        if late:
            out.append({"kind": "savings_interest", "severity": "info",
                        "text": "Interest is bijgeschreven: werk de saldi bij van " + ", ".join(late) +
                                " (centen manual balance …)."})
        out.append({"kind": "year_end_values", "severity": "info",
                    "text": f"Bewaar je jaaroverzichten en de waarden op 31/12/{today.year - 1} van je brokers; "
                            "importeer ze in centen."})
    if 4 <= today.month <= 7:
        out.append({"kind": "tax_return", "severity": "info",
                    "text": f"Aangifteperiode (inkomsten {today.year - 1}): het tabblad Fiscaal heeft je meerwaarden, "
                            "dividenden en wat je zelf moet aangeven."})
    try:
        from . import tax
        cg = tax.capital_gains(conn, today)
        if cg.get("missing_photo_prices"):
            out.append({"kind": "photo_price", "severity": "warning",
                        "text": "Fotomoment 31/12/2025 ontbreekt voor " + ", ".join(cg["missing_photo_prices"][:5]) +
                                ": geef de koers in (set_reference_price)."})
        cy = cg.get("current_year") or {}
        if today.month >= 11 and (cy.get("remaining") or 0) > 1000:
            out.append({"kind": "exemption_left", "severity": "info",
                        "text": f"Nog {_eur(cy['remaining'])} vrijstelling meerwaardebelasting over voor "
                                f"{today.year} (vervalt op 31/12)."})
    except Exception:  # noqa: BLE001
        pass
    try:
        from . import review
        q = review.queue(conn, "personal", 5, date_from=date(today.year, 1, 1).isoformat())
        if (q.get("share_of_spending") or 0) > 0.15:
            out.append({"kind": "unlabelled", "severity": "info",
                        "text": f"{round(q['share_of_spending'] * 100)} % van je uitgaven dit jaar staat nog onder "
                                "‘Overige’: labelen gaat per handelaar (tabblad Privé, ‘Nog te labelen’)."})
    except Exception:  # noqa: BLE001
        pass
    return out


def build(conn, today: date | None = None) -> dict:
    today = today or date.today()
    week_from = today - timedelta(days=7)
    rows = A.fetch(conn, week_from + timedelta(days=1), today, perspective="personal")
    spend = -sum(r["amount"] for r in rows if r["kind"] == "expense" and r["amount"] < 0 and not r["internal"]
                 and r["status"] == "booked")
    income = sum(r["amount"] for r in rows if r["kind"] == "income" and r["amount"] > 0 and not r["internal"]
                 and r["status"] == "booked")
    big = sorted([r for r in rows if r["kind"] == "expense" and r["amount"] < 0 and r["status"] == "booked"],
                 key=lambda r: r["amount"])[:5]
    out = {"today": today.isoformat(), "period": {"from": (week_from + timedelta(days=1)).isoformat(),
                                                 "to": today.isoformat()},
           "spending": r2(spend), "income": r2(income),
           "largest": [{"date": r["booking_date"], "merchant": r["merchant"], "amount": r2(r["amount"]),
                        "category": r["category"]} for r in big],
           "unusual": A.unusual(conn, 8, today)[:6], "upcoming": A.upcoming(conn, 14, today)["items"][:10],
           "reminders": reminders(conn, today)}
    try:
        from . import networth
        out["net_worth"] = networth.change_since(conn, 7, today)
    except Exception:  # noqa: BLE001
        out["net_worth"] = None
    lines = [f"Uitgaven {_eur(spend)} deze week"]
    if out["net_worth"]:
        lines.append(f"vermogen {'+' if out['net_worth']['change'] >= 0 else ''}{_eur(out['net_worth']['change'])}")
    if out["unusual"]:
        lines.append(f"{len(out['unusual'])} opvallend")
    if out["reminders"]:
        lines.append(f"{len(out['reminders'])} herinnering{'en' if len(out['reminders']) > 1 else ''}")
    out["headline"] = " · ".join(lines)
    return out


UNUSUAL_NL = {"possible_duplicate": "mogelijk dubbel", "higher_than_usual": "hoger dan gewoonlijk",
              "large_new_merchant": "grote betaling aan nieuwe begunstigde", "fee": "kosten of debetrente",
              "foreign_currency": "vreemde munt", "price_increase": "prijsstijging"}


def render_html(d: dict) -> str:
    e = html.escape

    def li(items):
        return "".join(f"<li>{x}</li>" for x in items) or "<li class=m>Niets.</li>"

    nw = d.get("net_worth")
    parts = [f"<h1>Weekoverzicht t.e.m. {e(d['today'])}</h1><p class=h>{e(d['headline'])}</p>"]
    parts.append("<h2>Deze week</h2><ul>" + li([
        f"Uitgaven {_eur(d['spending'])}, inkomsten {_eur(d['income'])}",
        *( [f"Vermogen {_eur(nw['now'])} ({'+' if nw['change'] >= 0 else ''}{_eur(nw['change'])} sinds {e(nw['from'])})"]
           if nw else [])]) + "</ul>")
    parts.append("<h2>Grootste uitgaven</h2><ul>" + li(
        [f"{e(x['date'])} · {e(x['merchant'] or '?')} · {_eur(x['amount'])}" for x in d["largest"]]) + "</ul>")
    parts.append("<h2>Opvallend</h2><ul>" + li(
        [f"{e(x.get('date') or '')} · {e(x.get('merchant') or '?')} · {e(UNUSUAL_NL.get(x.get('type'), x.get('type') or ''))} · {_eur(x.get('amount'))}"
         for x in d["unusual"]]) + "</ul>")
    parts.append("<h2>Komende 14 dagen</h2><ul>" + li(
        [f"{e(x.get('date') or '')} · {e(x.get('merchant') or '?')} · {_eur(x.get('amount'))}" for x in d["upcoming"]])
        + "</ul>")
    parts.append("<h2>Herinneringen</h2><ul>" + li([e(r["text"]) for r in d["reminders"]]) + "</ul>")
    css = ("body{font:15px system-ui,Segoe UI,sans-serif;max-width:720px;margin:32px auto;padding:0 16px;color:#0b0b0b}"
           "h1{font-size:22px}h2{font-size:15px;margin-top:22px;color:#52514e}.h{font-size:17px}li{margin:4px 0}"
           ".m{color:#898781}@media(prefers-color-scheme:dark){body{background:#141413;color:#f0efec}h2{color:#c3c2b7}}")
    return f"<!doctype html><html lang=nl><meta charset=utf-8><title>centen weekoverzicht</title><style>{css}</style>" \
           f"<body>{''.join(parts)}<p class=m>Lokaal opgebouwd door centen; niets verlaat je computer.</p></body></html>"


def write(conn, today: date | None = None, folder: Path | None = None) -> tuple[dict, Path]:
    from . import config
    d = build(conn, today)
    p = (folder or config.home()) / "digest.html"
    p.write_text(render_html(d), encoding="utf-8")
    (p.with_suffix(".json")).write_text(json.dumps(d, ensure_ascii=False, default=str, indent=1), encoding="utf-8")
    return d, p


_TOAST = r"""
$ErrorActionPreference = 'Stop'
try {
  [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
  [Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
  $x = New-Object Windows.Data.Xml.Dom.XmlDocument
  $x.LoadXml("<toast activationType='protocol' launch='" + $env:CENTEN_URI + "'><visual><binding template='ToastGeneric'><text>centen</text><text>" + [Security.SecurityElement]::Escape($env:CENTEN_MSG) + "</text></binding></visual></toast>")
  $app = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
  [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app).Show([Windows.UI.Notifications.ToastNotification]::new($x))
} catch {
  Add-Type -AssemblyName System.Windows.Forms
  $n = New-Object System.Windows.Forms.NotifyIcon
  $n.Icon = [System.Drawing.SystemIcons]::Information
  $n.Visible = $true
  $n.ShowBalloonTip(10000, 'centen', $env:CENTEN_MSG, 'Info')
  Start-Sleep -Seconds 11
  $n.Dispose()
}
"""


def notify(message: str, uri: str | None = None) -> bool:
    """A Windows notification (toast, else a tray balloon). Elsewhere it just prints. Never raises."""
    if sys.platform != "win32":
        print(message)
        return False
    import os
    try:
        env = {**os.environ, "CENTEN_MSG": message[:240], "CENTEN_URI": uri or ""}
        subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command",
                        _TOAST], env=env, timeout=30, capture_output=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return True
    except Exception:  # noqa: BLE001
        return False
