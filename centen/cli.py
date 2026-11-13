"""Command line: setup, bank linking, sync, file import, rules, demo data and the MCP server."""
from __future__ import annotations

import argparse
import http.server
import json
import os
import sys
import threading
import webbrowser
from contextlib import closing
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

from . import __version__, analytics as A, categorize, config, db


def _print(obj):
    print(json.dumps(obj, indent=2, ensure_ascii=False, default=str))


def cmd_init(a):
    cfg = config.load()
    eb = cfg["enablebanking"]
    eb["app_id"] = a.app_id or eb["app_id"] or input("Enable Banking application ID (UUID): ").strip()
    key = a.key or eb["private_key_path"] or input("Path to the application's private key (.pem): ").strip()
    key_path = Path(key).expanduser().resolve()
    if not key_path.is_file():
        sys.exit(f"Key file not found: {key_path}")
    try:
        key_path.chmod(0o600)
    except OSError:
        pass
    eb["private_key_path"] = str(key_path)
    if a.redirect_url:
        eb["redirect_url"] = a.redirect_url
    if a.country:
        eb["country"] = a.country.upper()
    if a.payday_day:
        cfg["payday_day"] = a.payday_day
    p = config.save(cfg)
    print(f"Saved {p}")
    from .enablebanking import Client
    try:
        app = Client.from_config(cfg).application()
        print(f"Connected to Enable Banking as application '{app.get('name')}' "
              f"({app.get('environment', '?')}, active={app.get('active')}).")
    except Exception as e:  # noqa: BLE001
        print(f"Saved, but the test call failed: {e}")


def cmd_banks(a):
    from .enablebanking import Client
    banks = Client.from_config(config.load()).aspsps(a.country or config.load()["enablebanking"]["country"])
    for b in sorted(banks, key=lambda b: b["name"]):
        days = int(b.get("maximum_consent_validity") or 0) // 86400
        print(f"{b['name']:<40} consent up to {days} days" + ("  (beta)" if b.get("beta") else ""))


def _catch_redirect(redirect_url: str, timeout: int = 600) -> str | None:
    u = urlparse(redirect_url)
    if u.scheme != "http" or u.hostname not in ("127.0.0.1", "localhost") or not u.port:
        return None
    got: dict = {}

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            got["url"] = f"http://{u.hostname}:{u.port}{self.path}"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write("<h2>Bank linked to centen.</h2><p>You can close this tab and go back to the terminal.</p>"
                             .encode())

        def log_message(self, *args):
            pass

    srv = http.server.HTTPServer((u.hostname, u.port), H)
    srv.timeout = timeout
    t = threading.Thread(target=srv.handle_request, daemon=True)
    t.start()
    t.join(timeout)
    srv.server_close()
    return got.get("url")


def cmd_link(a):
    from . import enablebanking as eb
    cfg = config.load()
    with closing(db.connect()) as conn:
        info = eb.start_link(conn, cfg, a.bank, a.country)
        print(f"\nOpen this link, choose your account(s) and approve with your bank app or card reader:\n\n  {info['url']}\n")
        print(f"Access is read-only and valid until {info['valid_until'][:10]}.")
        try:
            webbrowser.open(info["url"])
        except Exception:  # noqa: BLE001
            pass
        redirect = None
        url = cfg["enablebanking"]["redirect_url"]
        if not a.paste and urlparse(url).scheme == "http":
            print(f"Waiting for the redirect to {url} ...")
            redirect = _catch_redirect(url)
        if not redirect:
            redirect = input(f"\nAfter approving, your browser goes to {url} and shows a \"can't connect\" page. "
                             "That's expected.\nCopy the full address from the address bar (it contains ?code=...) "
                             "and paste it here: ").strip()
        result = eb.complete_link(conn, cfg, redirect)
        _print(result)
        if not a.no_sync:
            print("Fetching transactions ...")
            _print(eb.sync(conn, cfg, force=True, days=a.days))
            _dashboard_note(conn)


def cmd_sync(a):
    from . import enablebanking as eb
    with closing(db.connect()) as conn:
        _print(eb.sync(conn, config.load(), force=a.force, days=a.days))
        _prices(conn)
        _dashboard_note(conn)


def cmd_unlink(a):
    from . import enablebanking as eb
    with closing(db.connect()) as conn:
        _print(eb.unlink(conn, config.load(), a.link_id))


def cmd_import(a):
    from .importers import import_file, import_history
    with closing(db.connect()) as conn:
        if a.history:
            res = import_history(conn, a.files, account_name=a.account, bank=a.bank,
                                 only_before_linked=not a.include_linked_period)
            for r in res["files"]:
                if "error" in r:
                    print(f"{r['file']}: {r['error']}")
                    continue
                print(f"{r['file']} ({r['format']}): {r['new_transactions']} new, {r['already_present']} already "
                      f"present, {r['skipped_as_synced_elsewhere']} duplicates, {r['rows_from_linked_period_left_out']} "
                      "left out (period the bank link already covers)")
            _dashboard_note(conn)
            return
        for f in a.files:
            res = import_file(conn, f, account_name=a.account, bank=a.bank, account_type=a.type)
            print(f"{f}: {res['new_transactions']} new, {res['already_present']} already present, "
                  f"{res['skipped_as_synced_elsewhere']} skipped (already synced from the bank)")
        _dashboard_note(conn)


def _prices(conn, force=False, quiet=True):
    from . import history, prices
    try:
        res = prices.refresh(conn, max_age_minutes=0 if force else 15)
    except Exception as e:  # noqa: BLE001
        res = {"error": str(e)}
    try:  # once a day: 31/12/2025 and month-end prices (tax view, net worth history, benchmark)
        history.ensure_history(conn)
    except Exception:  # noqa: BLE001
        pass
    if not quiet:
        _print(res)
    return res


def cmd_prices(a):
    from . import prices
    with closing(db.connect()) as conn:
        for item in a.set or []:
            key, _, sym = item.partition("=")
            _print(prices.set_symbol(conn, key, sym or None))
        _prices(conn, force=True, quiet=getattr(a, "quiet", False))
        _dashboard_note(conn)


def _dashboard_note(conn):
    from . import dashboard
    p = dashboard.refresh(conn)
    if p:
        print(f"Dashboard updated: {p}")


def cmd_fixed(a):
    from . import fixed
    with closing(db.connect()) as conn:
        if a.action == "add":
            _print(fixed.add_manual(conn, a.name, a.amount, a.cadence, a.next, a.group, a.scope, a.paid_by))
            return _dashboard_note(conn)
        if a.action in ("ignore", "cancel", "confirm", "restore"):
            st = {"ignore": "ignored", "cancel": "cancelled", "confirm": "confirmed", "restore": "active"}[a.action]
            _print(fixed.set_item(conn, a.name, st, scope=a.scope))
            return _dashboard_note(conn)
        rep = fixed.report(conn, perspective=a.scope)
    if a.json:
        return _print(rep)
    for sc, t in rep["totals"].items():
        print(f"{'Privé' if sc == 'personal' else 'BV'}: {_eur(t['monthly'])} per maand · {_eur(t['yearly'])} per jaar "
              f"({t['count']} vaste kosten)")
    cur = None
    for i in rep["items"]:
        if i["status"] != "active":
            continue
        if (i["scope"], i["group"]) != cur:
            cur = (i["scope"], i["group"])
            print(f"\n  {i['group']} ({i['scope']})")
        print(f"    {i['name'][:38]:38} {i['cadence']:>14} {_eur(i['amount'])} {_eur(i['yearly'])}/jaar  "
              f"volgende {i['next_date'] or '—'}")
    for x in rep["price_changes"]:
        print(f"  prijs {x['name']}: {x['from']} → {x['to']} ({x['pct']:+} %) sinds {x['since']}")
    for x in rep["stopped"]:
        print(f"  gestopt? {x['name']}: laatste betaling {x['last_date']}")


def cmd_forecast(a):
    from . import forecast
    with closing(db.connect()) as conn:
        f = forecast.forecast(conn, months=a.months)
    if a.json:
        return _print(f)
    for p, v in f["perspectives"].items():
        if v:
            print(f"{p:9} nu {_eur(v['start'])}  einde {_eur(v['end'])}  laagste {_eur(v['min'])} op {v['min_date']}")
    for acc in f["accounts"]:
        print(f"  {acc['name'][:30]:30} {_eur(acc['start'])} → {_eur(acc['end'])}  laagste {_eur(acc['min'])} op {acc['min_date']}")
    for w in f["warnings"]:
        print("  ! " + w["text"])


def cmd_pension(a):
    from . import pension
    with closing(db.connect()) as conn:
        if a.one_off or a.keep:
            pension.mark_one_off(conn, a.one_off or a.keep, True if a.one_off else False)
        if any(x is not None for x in (a.birth_date, a.legal, a.age, a.spending, a.investing, a.stop_at)) or a.auto:
            pension.set_profile(conn, birth_date=a.birth_date, legal_pension_monthly=a.legal, retirement_age=a.age,
                                stop_work_age=a.stop_at,
                                spending_monthly=a.spending, investing_yearly=a.investing, reset=a.auto or None)
        o = pension.overview(conn)
    if a.json:
        return _print(o)
    print(f"Pensioen op {o['retirement_date'] or '?'} (nog {o['years_to_retirement'] or '?'} jaar)")
    for p in o["pillars"]:
        print(f"  {p['label']:48} nu {_eur(p.get('value_now'))}  per maand later {_eur(p.get('monthly_today_eur'))}")
    print(f"  samen per maand (euro van vandaag) {_eur(o['monthly_income_today_eur'])}  "
          f"uitgaven nu {_eur(o['spending_monthly_now'])}")
    sp, inv = o["spending_profile"], o["investing"]
    print(f"  uitgavenprofiel {_eur(sp['monthly'])}/maand{' (zelf ingesteld)' if sp['override'] else ''}; "
          f"niet meegeteld: {_eur(sp['one_offs_total'])}")
    for x in sp["one_offs"]:
        print(f"    {x['date']} {(x['merchant'] or '')[:30]:30} {_eur(x['amount'])} ({x['reason']}) id {x['id']}")
    print(f"  nieuwe inleg beleggingen {_eur(inv['yearly'])}/jaar{' (zelf ingesteld)' if inv['override'] else ''}")
    for c in o["checks"]:
        print("  · " + c["text"])


def cmd_plan(a):
    from . import planner
    with closing(db.connect()) as conn:
        r = planner.plan(conn, stop_work_age=a.stop_at, extra_monthly_saving=a.extra or 0.0,
                         spending_change_pct=(a.spend_change or 0.0) / 100, return_=a.ret / 100 if a.ret else None)
    if a.json:
        return _print(r)
    p = r["projection"]
    print(f"Financieel onafhankelijk: {p['fi_year'] or 'niet binnen de periode'}"
          f"{' (leeftijd ' + str(p['fi_age']) + ')' if p['fi_age'] else ''} · vermogen bij pensioen {_eur(p['capital_at_retirement'])}")
    for s in r["scenarios"]:
        print(f"  {s['name']:32} FI {s['fi_year'] or '—'}  bij pensioen {_eur(s['capital_at_retirement'])}")


def cmd_notify(a):
    from . import notify
    if a.action == "setup":
        return _print(notify.setup(a.topic, a.url, a.token, a.detail, a.public_url))
    if a.action == "test":
        return _print(notify.test())
    with closing(db.connect()) as conn:
        _print(notify.run_alerts(conn, dry_run=a.action == "preview"))


def cmd_dashboard(a):
    from . import dashboard
    with closing(db.connect()) as conn:
        if not a.no_prices:
            _prices(conn)
        p = dashboard.build(conn, a.out)
    print(f"Dashboard written to {p}")
    if not a.no_open:
        try:
            webbrowser.open(Path(p).resolve().as_uri())
        except Exception:  # noqa: BLE001
            pass


def cmd_accounts(a):
    with closing(db.connect()) as conn:
        for acc in A.accounts_overview(conn):
            bal = acc["available"] if acc["available"] is not None else acc["balance"]
            print(f"{acc['name']:<28} {acc['bank'] or '':<16} {acc['scope']:<9} {acc['type']:<8} {acc['account'] or '':<9} "
                  f"{'' if bal is None else f'{bal:>12,.2f}'}  {acc['transactions']} tx  "
                  f"{acc['history_from'] or ''} → {acc['history_to'] or ''}")
        links = conn.execute("SELECT id, aspsp_name, status, valid_until FROM bank_links").fetchall()
        for l in links:
            print(f"link {l['id']}  {l['aspsp_name']}  {l['status']}  valid until {(l['valid_until'] or '')[:10]}")


def cmd_manual(a):
    from . import manual
    with closing(db.connect()) as conn:
        try:
            if a.action == "suggest":
                _print(manual.suggestions(conn))
            elif a.action == "add":
                _print(manual.add_account(conn, a.iban, _amount(a.balance), a.date, a.name, a.bank, a.scope, a.type))
            elif a.action == "balance":
                _print(manual.set_balance(conn, a.account, _amount(a.balance), a.date))
            elif a.action == "remove":
                _print(manual.remove(conn, a.account))
            else:
                _print(manual.list_accounts(conn))
        except ValueError as e:
            sys.exit(str(e))
        if a.action in ("add", "balance", "remove"):
            _dashboard_note(conn)


def cmd_asset(a):
    from . import assets
    with closing(db.connect()) as conn:
        try:
            if a.action == "suggest":
                _print(assets.suggestions(conn))
            elif a.action == "add":
                _print(assets.add_account(conn, a.name, a.type, _amount(a.value) if a.value else None, a.date,
                                          a.provider, a.scope, a.match, a.account, _amount(a.inleg) if a.inleg else None,
                                          a.inleg_date))
            elif a.action == "value":
                _print(assets.set_value(conn, a.account, _amount(a.value), a.date))
            elif a.action == "update":
                _print(assets.update_account(conn, a.account, a.name, a.provider, a.scope, a.type, a.match,
                                             a.unmatch, a.add_account, a.remove_account,
                                             _amount(a.inleg) if a.inleg else None, a.inleg_date))
            elif a.action == "positions":
                _print(assets.import_positions(conn, a.account, a.file, a.date, not a.keep_value))
            elif a.action == "import":
                from . import portfolio
                _print(portfolio.import_files(conn, a.account, a.paths, a.force))
            elif a.action == "remove":
                _print(assets.remove(conn, a.account))
            else:
                _print(assets.summary(conn))
        except ValueError as e:
            sys.exit(str(e))
        if a.action in ("add", "value", "update", "positions", "import", "remove"):
            _dashboard_note(conn)


def cmd_portfolio(a):
    from . import portfolio
    with closing(db.connect()) as conn:
        try:
            res = portfolio.summary(conn, a.account, include_closed=a.closed)
        except ValueError as e:
            sys.exit(str(e))
    if a.json:
        _print(res)
        return
    f = lambda v: " " * 12 if v is None else f"{v:>12,.2f}"  # noqa: E731
    for acc in res["accounts"]:
        t = acc["totals"]
        irr = "" if t["xirr"] is None else f"{t['xirr']:.1%}"
        print(f"\n{acc['account']}  value {f(t['value']).strip()}  unrealised {f(t['unrealised']).strip()}  "
              f"realised {f(t['realised']).strip()}  XIRR {irr}")
        print(f"  {'holding':<42}{'qty':>12}{'avg cost':>12}{'cost':>12}{'value':>12}{'+/-':>12}")
        for h in acc["holdings"]:
            qty = " " * 12 if h.get("quantity") is None else f"{h['quantity']:>12,.6g}"
            print(f"  {(h['name'] or '')[:41]:<42}{qty}{f(h.get('avg_cost'))}{f(h.get('cost'))}"
                  f"{f(h.get('value'))}{f(h.get('unrealised'))}")
        for w in acc["warnings"]:
            print(f"  ! {w}")
    if res["years"]:
        print(f"\n{'year':<6}{'deposits':>12}{'withdrawn':>12}{'realised':>12}{'dividends':>12}{'div. tax':>12}"
              f"{'costs':>12}{'TOB':>12}")
        for y, d in res["years"].items():
            print(f"{y:<6}{f(d['deposits'])}{f(d['withdrawals'])}{f(d['realised'])}{f(d['dividends'])}"
                  f"{f(d['dividend_tax'])}{f(d['trade_costs'] + d['other_costs'])}{f(d['tax'])}")


def _amount(s: str) -> float:
    import re
    from .money import parse_amount
    t = s.strip().replace("€", "").replace(" ", "")
    if re.fullmatch(r"-?\d{1,3}(\.\d{3})+", t):  # "40.000" typed the Belgian way means forty thousand
        t = t.replace(".", "")
    v = parse_amount(t)
    if v is None:
        sys.exit(f"Not an amount: {s}")
    return v


def _eur(v) -> str:
    return "—" if v is None else f"{v:>12,.2f}".replace(",", " ")


def cmd_tax(a):
    from . import history, tax
    with closing(db.connect()) as conn:
        if a.fetch:
            history.ensure_history(conn, force=True)
        for item in a.set or []:
            key, _, px = item.partition("=")
            _print(history.set_reference(conn, key.strip().upper(), _amount(px)))
        rep = tax.report(conn, year=a.year)
        if a.json:
            _print(rep)
            return
        cg = rep["capital_gains"]
        print(f"Meerwaardebelasting (10 %, FIFO, fotomoment 31/12/2025) — rekenhulp, geen fiscaal advies")
        for y in cg["years"]:
            print(f"  {y['year']}: {y['sales']} verkopen, winst {_eur(y['gains'])}, verlies {_eur(y['losses'])}, netto "
                  f"{_eur(y['net_gain'])} | vrijstelling {_eur(y['available'])}, over {_eur(y['remaining'])}, "
                  f"belastbaar {_eur(y['taxable'])} → ±{_eur(y['tax_estimate'])}")
        lt = cg["latent_total"]
        print(f"  Latente meerwaarde vandaag {_eur(lt['gain'])} op {_eur(lt['value'])}; belasting als alles vandaag "
              f"verkocht zou worden ±{_eur(lt['tax_if_sold_today'])}")
        for i in cg["use_exemption_ideas"][:4]:
            print(f"  Vrijstelling benutten: {i['units']:g} × {i['instrument']} ({i['account']}) → meerwaarde "
                  f"{_eur(i['gain'])}")
        if cg["missing_photo_prices"]:
            print("  ! Fotomoment ontbreekt: " + ", ".join(cg["missing_photo_prices"]) +
                  "  (centen tax --fetch, of --set ISIN=koers)")
        for d in rep["dividends"]["years"][-2:]:
            print(f"  Dividenden {d['year']}: bruto {_eur(d['gross'])}, buitenlands {_eur(d['foreign_gross'])} "
                  f"(RV aan te geven ±{_eur(d['rv_due_on_foreign_estimate'])}), TOB {_eur(d['tob'])}")
        print("Checklist:")
        for c in rep["checklist"]:
            print(f"  - {c}")


def cmd_networth(a):
    from . import networth
    with closing(db.connect()) as conn:
        if a.rebuild:
            _print(networth.reconstruct(conn))
        tl = networth.timeline(conn, a.months)
    if a.json:
        _print(tl)
        return
    for p in tl["points"]:
        est = " (geschat: " + ", ".join(p["carried_back"]) + ")" if p.get("carried_back") else ""
        print(f"{p['date']}  {_eur(p['total'])}  cash {_eur((p['cash_personal'] or 0) + (p['cash_business'] or 0))}  "
              f"beleggingen {_eur(p['investments'])}  {p['source']}{est}")


def cmd_label(a):
    from . import review
    with closing(db.connect()) as conn:
        if a.merchant and a.category:
            _print(review.label(conn, a.merchant, a.category, a.scope, dry_run=a.dry_run))
            if not a.dry_run:
                _dashboard_note(conn)
            return
        q = review.queue(conn, a.scope or "personal", a.limit)
    print(f"{q['transactions']} betalingen in {q['groups']} groepen onder 'Overige' "
          f"({'—' if q['share_of_spending'] is None else round(q['share_of_spending'] * 100)} % van de uitgaven)")
    for i in q["items"]:
        sug = i["suggestion"] or {}
        print(f"  {i['merchant'][:34]:<35}{i['count']:>4}×{_eur(i['total'])}   "
              f"{('→ ' + sug['category']) if sug.get('category') else (sug.get('reason') or '')}")
    print('Labelen: centen label "<handelaar>" "<categorie>"   (centen categories toont de categorieën)')


def cmd_analysis(a):
    from . import analysis
    with closing(db.connect()) as conn:
        for item in a.target or []:
            dim, _, rest = item.partition(":")
            pairs = dict(x.split("=", 1) for x in rest.split(",") if "=" in x)
            _print(analysis.set_targets(conn, dim, {k: float(v) for k, v in pairs.items()}))
        rep = analysis.report(conn, new_money=a.new_money or 0.0)
    if a.json:
        _print(rep)
        return
    for d, v in rep["allocation"]["dimensions"].items():
        print(f"\n{d}:")
        for r in v["rows"]:
            t = f"  doel {r['target']:.0%}  afwijking {r['deviation']:+.1%}" if "target" in r else ""
            print(f"  {r['bucket'][:26]:<27}{_eur(r['value'])}  {r['weight']:>6.1%}{t}")
        if v.get("plan") and a.new_money:
            print("  bijkopen met nieuw geld: " + ", ".join(f"{k} {x:,.0f}" for k, x in
                                                          v["plan"]["buy_with_new_money"].items() if x))
    dv = rep["dividends"]
    print(f"\nVerwachte dividenden komende 12 maanden: {_eur(dv['total_12m'])} bruto")
    b = rep["benchmark"]
    if b.get("available"):
        print(f"Benchmark {b['benchmark']['name']}: jouw waarde {_eur(b['value'])} vs {_eur(b['benchmark_value'])} "
              f"(XIRR {b['xirr']:.1%} vs {b['benchmark_xirr']:.1%})" if b.get("xirr") is not None and
              b.get("benchmark_xirr") is not None else f"Benchmark: {_eur(b['value'])} vs {_eur(b['benchmark_value'])}")
    else:
        print(b.get("note"))


def cmd_digest(a):
    from . import digest
    with closing(db.connect()) as conn:
        d, p = digest.write(conn)
    print(d["headline"])
    for r in d["reminders"]:
        print(f"  - {r['text']}")
    print(f"Weekoverzicht: {p}")
    if a.notify:
        digest.notify(d["headline"], p.resolve().as_uri())
    if a.open:
        webbrowser.open(p.resolve().as_uri())


def cmd_schedule(a):
    from . import schedule
    if a.action == "install":
        _print(schedule.install(dry_run=a.dry_run))
    elif a.action == "remove":
        _print(schedule.remove())
    else:
        _print(schedule.show())


def cmd_loan(a):
    from . import loans
    with closing(db.connect()) as conn:
        if a.account and a.outstanding is not None:
            _print(loans.set_terms(conn, a.account, _amount(a.outstanding), a.maturity, float(a.rate.replace(",", ".")),
                                   _amount(a.principal) if a.principal else None, a.contract, a.statement_date))
            _dashboard_note(conn)
            return
        o = loans.overview(conn)
    print(f"Woning {_eur(o['property_value'])}  leningen {_eur(o['owed'])}  netto {_eur(o['equity'])}  "
          f"afbetaling/maand {_eur(o['monthly_instalments'])}  schuldenvrij {o['debt_free_on']}")
    for x in o["loans"]:
        n = x.get("next_split") or {}
        print(f"  {x['account']:<22} nog {_eur(x['outstanding_now'])}  {x['payments_left']} afbetalingen van "
              f"{_eur(x['instalment'])} (volgende: rente {_eur(n.get('interest'))}, kapitaal {_eur(n.get('capital'))})"
              f"  dit jaar rente {_eur(x['this_year']['interest'])}, kapitaal {_eur(x['this_year']['capital'])}")


def cmd_app(a):
    from . import app
    app.start(port=a.port, open_browser=not a.no_open, block=True)


def cmd_web(a):
    from . import web
    if a.show_key:
        print(web.access_key())
        return
    web.serve(a.host, a.port, run_scheduler=not a.no_scheduler)


def cmd_remote(a):
    from . import remote
    remote.main(a.url, a.key)


def cmd_backup(a):
    import sqlite3
    out = Path(a.path).expanduser()
    if out.is_dir():
        out = out / f"centen-{date.today().isoformat()}.db"
    with closing(db.connect()) as conn, closing(sqlite3.connect(out)) as dst:
        conn.backup(dst)  # consistent copy, also while Claude or the server is using the database
    print(f"Backup written to {out}")


COMPOSE_NOTE = "# made by centen nas-export; see README 'Run it on a NAS or home server'\n"


def cmd_nas_export(a):
    """Everything a server needs, in one folder: compose.yaml, .env with a new access key, and data/ with a consistent
    copy of the database, config.json and the Enable Banking key. Copy the folder to the NAS and start it."""
    import secrets
    import shutil
    import sqlite3
    from importlib import resources
    out = Path(a.folder).expanduser()
    data = out / "data"
    data.mkdir(parents=True, exist_ok=True)
    (out / "imports").mkdir(exist_ok=True)
    with closing(db.connect()) as conn, closing(sqlite3.connect(data / "centen.db")) as dst:
        conn.backup(dst)
    cfg = config.load()
    key_src = Path(cfg["enablebanking"].get("private_key_path") or "")
    if key_src.is_file():
        shutil.copy2(key_src, data / key_src.name)
        cfg["enablebanking"]["private_key_path"] = f"/data/{key_src.name}"
    (data / "config.json").write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    key = secrets.token_urlsafe(24)
    env = out / ".env"
    if not env.exists():
        env.write_text(f"CENTEN_KEY={key}\n", encoding="utf-8")
    else:
        key = next((l.split("=", 1)[1].strip() for l in env.read_text(encoding="utf-8").splitlines()
                    if l.startswith("CENTEN_KEY=")), key)
    tpl = resources.files("centen").joinpath("compose.yaml").read_text(encoding="utf-8")
    (out / "compose.yaml").write_text(COMPOSE_NOTE + tpl, encoding="utf-8")
    print(f"Ready in {out}: compose.yaml, .env (access key), data/ (database, config.json"
          f"{', bank key' if key_src.is_file() else ''}).")
    print("1. Copy this folder to the NAS (e.g. /volume1/docker/centen).")
    print("2. Container Manager > Project > Create > choose that folder (or: docker compose up -d).")
    print(f"3. Open http://<nas>:8770 and log in with the key in .env.")
    print("Then stop syncing from this computer: point Claude Desktop at the NAS with `centen remote`.")


def cmd_scope(a):
    with closing(db.connect()) as conn:
        ids = A.resolve_accounts(conn, a.account)
        if len(ids) != 1:
            sys.exit(f"'{a.account}' matches {len(ids)} accounts; be more specific.")
        _print(categorize.set_scope(conn, ids[0], a.scope))


def cmd_member(a):
    cfg = config.load()
    members = dict(cfg.get("household_members") or {})
    if a.action == "add":
        members[a.name] = a.aliases or [a.name]
    elif a.action == "remove":
        members.pop(a.name, None)
    if a.action != "list":
        cfg["household_members"] = members
        config.save(cfg)
        with closing(db.connect()) as conn:
            categorize.recategorize(conn)
    _print(members)


def cmd_rules(a):
    with closing(db.connect()) as conn:
        if a.action == "list":
            for r in conn.execute("SELECT * FROM category_rules ORDER BY id"):
                print(f"{r['id']:>3}  {r['pattern']!r:<40} {r['direction']:<6} → {r['category']}"
                      + (f"  (as {r['merchant']})" if r["merchant"] else ""))
        elif a.action == "add":
            _print(categorize.add_rule(conn, a.pattern, a.category, a.direction, a.merchant))
        elif a.action == "delete":
            conn.execute("DELETE FROM category_rules WHERE id=?", (a.id,))
            conn.commit()
            print(f"Deleted rule {a.id}; re-categorised {categorize.recategorize(conn)} transactions.")


def cmd_recategorize(a):
    with closing(db.connect()) as conn:
        print(f"Re-categorised {categorize.recategorize(conn)} transactions (hand-picked categories kept).")


def cmd_categories(a):
    for name, kind, grp in categorize.CATEGORIES:
        print(f"{grp:<20} {kind:<9} {name}")


def cmd_demo(a):
    path = Path(a.db).expanduser() if a.db else config.home() / "demo.db"
    if path.exists():
        if not a.force:
            sys.exit(f"{path} exists; use --force to regenerate.")
        for suffix in ("", "-wal", "-shm"):
            Path(str(path) + suffix).unlink(missing_ok=True)
    from .demo import generate
    with closing(db.connect(path)) as conn:
        res = generate(conn)
    print(f"Demo data written to {path}: {res['transactions']} transactions, {res['from']} → {res['to']}.")
    print(f"Try it in Claude by setting CENTEN_DB={path} in the MCP server's env (see README).")


def cmd_status(a):
    with closing(db.connect()) as conn:
        _print({"database": str(config.db_path()), "config": str(config.config_path()),
                "overview": A.overview(conn, date.today())})


def cmd_serve(a):
    from .server import main
    main()


def build_parser():
    p = argparse.ArgumentParser(prog="centen", description="Personal Finance for Claude, for Belgian bank accounts.")
    p.add_argument("--version", action="version", version=f"centen {__version__}")
    p.add_argument("--db", help="Database file (default ~/.centen/centen.db or $CENTEN_DB)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="Save Enable Banking credentials and test them")
    s.add_argument("--app-id")
    s.add_argument("--key", help="Path to the application's private key (.pem)")
    s.add_argument("--redirect-url", help="Redirect URL whitelisted for the application")
    s.add_argument("--country", help="Default country (BE)")
    s.add_argument("--payday-day", type=int, help="Day of the month you get paid (optional)")
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("banks", help="List banks available for linking")
    s.add_argument("--country")
    s.set_defaults(func=cmd_banks)

    s = sub.add_parser("link", help="Link a bank (read-only consent) and fetch history")
    s.add_argument("--bank", required=True, help='Bank name as listed by `centen banks`, e.g. "KBC"')
    s.add_argument("--country")
    s.add_argument("--days", type=int, default=730, help="History to request on first sync (banks may serve less)")
    s.add_argument("--paste", action="store_true", help="Don't listen for the redirect; paste the URL instead")
    s.add_argument("--no-sync", action="store_true")
    s.set_defaults(func=cmd_link)

    s = sub.add_parser("sync", help="Fetch new balances and transactions")
    s.add_argument("--force", action="store_true", help="Ignore min_sync_hours")
    s.add_argument("--days", type=int, default=730)
    s.set_defaults(func=cmd_sync)

    s = sub.add_parser("unlink", help="Revoke a bank link")
    s.add_argument("link_id")
    s.set_defaults(func=cmd_unlink)

    s = sub.add_parser("import", help="Import CSV, CAMT.053 (XML) or CODA exports from your bank")
    s.add_argument("files", nargs="+")
    s.add_argument("--history", action="store_true",
                   help="Older history for a linked account: leaves out what the bank link already covers")
    s.add_argument("--include-linked-period", action="store_true", help="With --history: also the linked period")
    s.add_argument("--account", help="Account name to use when the file has no account number column")
    s.add_argument("--bank")
    s.add_argument("--type", default="current", choices=["current", "savings", "card", "loan", "other"])
    s.set_defaults(func=cmd_import)

    sub.add_parser("accounts", help="Show accounts, balances and links").set_defaults(func=cmd_accounts)
    s = sub.add_parser("dashboard", help="Build the HTML dashboard (data folder/dashboard.html) and open it")
    s.add_argument("--out", help="Write to this file instead")
    s.add_argument("--no-open", action="store_true", help="Don't open it in the browser")
    s.add_argument("--no-prices", action="store_true", help="Don't fetch current prices first")
    s.set_defaults(func=cmd_dashboard)

    s = sub.add_parser("prices", help="Fetch current prices for your holdings (Yahoo Finance, Bitvavo)")
    s.add_argument("--set", action="append", metavar="ISIN=SYMBOL",
                   help="Fix the quote used, e.g. IE0031442068=IUSA.AS (empty symbol = automatic)")
    s.add_argument("--quiet", action="store_true", help="Don't print the prices (scheduled task)")
    s.set_defaults(func=cmd_prices)

    s = sub.add_parser("member", help="Household members whose own accounts aren't linked (e.g. your partner)")
    ms = s.add_subparsers(dest="action", required=True)
    ms.add_parser("list")
    m = ms.add_parser("add")
    m.add_argument("name")
    m.add_argument("aliases", nargs="*", help='Full names as on statements, e.g. "An Peeters"')
    m = ms.add_parser("remove")
    m.add_argument("name")
    s.set_defaults(func=cmd_member)

    s = sub.add_parser("manual", help="Savings accounts the bank doesn't share: add with a balance, centen tracks it")
    ms2 = s.add_subparsers(dest="action")
    ms2.add_parser("list")
    ms2.add_parser("suggest", help="Own-looking accounts seen in transfers that aren't linked")
    m = ms2.add_parser("add")
    m.add_argument("iban", help="Full IBAN or its last 4 digits")
    m.add_argument("balance", help="Current balance, e.g. 40123,45")
    m.add_argument("--date", help="Date of that balance (YYYY-MM-DD), default today")
    m.add_argument("--name")
    m.add_argument("--bank")
    m.add_argument("--scope", choices=["personal", "business"])
    m.add_argument("--type", default="savings", choices=["savings", "current", "other"])
    m = ms2.add_parser("balance", help="Enter a new balance (e.g. after interest)")
    m.add_argument("account", help="Name, bank or last 4 digits")
    m.add_argument("balance")
    m.add_argument("--date")
    m = ms2.add_parser("remove")
    m.add_argument("account")
    s.set_defaults(func=cmd_manual)

    s = sub.add_parser("asset", help="Investment, crypto, pension, property and loan accounts (value + payments)")
    as_ = s.add_subparsers(dest="action")
    as_.add_parser("list")
    as_.add_parser("suggest", help="Brokers and pension-like premiums seen in your payments")
    types = ["investment", "crypto", "pension", "insurance", "property", "asset", "loan"]
    m = as_.add_parser("add", help='e.g. centen asset add DEGIRO investment --value 150.000')
    m.add_argument("name")
    m.add_argument("type", choices=types)
    m.add_argument("--value", help="What it is worth (loan: amount still owed), e.g. 152.340,12")
    m.add_argument("--date", help="Date of that value (YYYY-MM-DD), default today")
    m.add_argument("--provider", help="Broker, insurer or bank")
    m.add_argument("--scope", choices=["personal", "business"])
    m.add_argument("--match", action="append", help="Text that recognises its payments (repeatable)")
    m.add_argument("--account", action="append", help="Counterparty IBAN or last 4 digits of its payments (repeatable)")
    m.add_argument("--inleg", help="Total contributions up to --inleg-date (money put in before your bank history)")
    m.add_argument("--inleg-date")
    m = as_.add_parser("value", help="Enter a new value (from the broker app or statement)")
    m.add_argument("account", help="Name, provider or type")
    m.add_argument("value")
    m.add_argument("--date")
    m = as_.add_parser("update")
    m.add_argument("account")
    m.add_argument("--name")
    m.add_argument("--provider")
    m.add_argument("--scope", choices=["personal", "business"])
    m.add_argument("--type", choices=types)
    m.add_argument("--match", action="append")
    m.add_argument("--unmatch", action="append", help="Remove a text pattern")
    m.add_argument("--add-account", action="append")
    m.add_argument("--remove-account", action="append")
    m.add_argument("--inleg")
    m.add_argument("--inleg-date")
    m = as_.add_parser("positions", help="Import holdings: DEGIRO Portfolio.csv, LYNX/IBKR statement CSV, or a table")
    m.add_argument("account")
    m.add_argument("file")
    m.add_argument("--date", help="Date of the positions (default: statement date or today)")
    m.add_argument("--keep-value", action="store_true", help="Don't use their total as the account value")
    m = as_.add_parser("import", help="Broker exports: DEGIRO Transactions/Account/Portfolio.csv, LYNX/IBKR, Bitvavo")
    m.add_argument("account")
    m.add_argument("paths", nargs="+", help="CSV files or folders")
    m.add_argument("--force", action="store_true", help="Import even if the file looks like another broker's")
    m = as_.add_parser("remove")
    m.add_argument("account")
    s.set_defaults(func=cmd_asset)

    s = sub.add_parser("portfolio", help="Holdings, average cost, results, dividends, costs and XIRR from broker exports")
    s.add_argument("account", nargs="?")
    s.add_argument("--closed", action="store_true", help="Include sold-out positions")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_portfolio)

    s = sub.add_parser("scope", help="Mark an account as personal or business")
    s.add_argument("account", help="Part of the account name, bank, or last 4 digits")
    s.add_argument("scope", choices=["personal", "business"])
    s.set_defaults(func=cmd_scope)

    s = sub.add_parser("rules", help="Your own categorisation rules")
    rs = s.add_subparsers(dest="action", required=True)
    rs.add_parser("list")
    r = rs.add_parser("add")
    r.add_argument("pattern")
    r.add_argument("category")
    r.add_argument("--direction", default="any", choices=["any", "debit", "credit"])
    r.add_argument("--merchant", help="Display name to assign")
    r = rs.add_parser("delete")
    r.add_argument("id", type=int)
    s.set_defaults(func=cmd_rules)

    sub.add_parser("recategorize", help="Re-run categorisation rules").set_defaults(func=cmd_recategorize)
    sub.add_parser("categories", help="List categories").set_defaults(func=cmd_categories)

    s = sub.add_parser("demo", help="Create a demo database (fictional Belgian household) at --db or ~/.centen/demo.db")
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_demo)


    s = sub.add_parser("tax", help="Belgian tax view: capital gains tax 2026+, dividends/RV, TOB, Reynders, checklist")
    s.add_argument("--year", type=int)
    s.add_argument("--fetch", action="store_true", help="Fetch 31/12/2025 and month-end prices first")
    s.add_argument("--set", action="append", metavar="ISIN=PRICE", help="Enter a 31/12/2025 price by hand (EUR)")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_tax)

    s = sub.add_parser("fixed", help="Subscriptions and fixed costs (r17)")
    s.add_argument("action", nargs="?", default="list", choices=["list", "add", "ignore", "cancel", "confirm", "restore"])
    s.add_argument("name", nargs="?")
    s.add_argument("amount", nargs="?", type=float)
    s.add_argument("cadence", nargs="?", default="monthly")
    s.add_argument("--next", help="next payment date YYYY-MM-DD (add)")
    s.add_argument("--group")
    s.add_argument("--scope", choices=["personal", "business"])
    s.add_argument("--paid-by", dest="paid_by")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_fixed)

    s = sub.add_parser("forecast", help="Expected balances for the coming months (r17)")
    s.add_argument("--months", type=int, default=6)
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_forecast)

    s = sub.add_parser("pension", help="Pension overview, four pillars (r17)")
    s.add_argument("--birth-date", dest="birth_date")
    s.add_argument("--legal", type=float, help="legal pension per month from mypension.be")
    s.add_argument("--age", type=int, help="retirement age (default 67)")
    s.add_argument("--spending", type=float, help="spending per month for the projection (instead of history)")
    s.add_argument("--investing", type=float, help="new money into investments per year (instead of history)")
    s.add_argument("--stop-at", dest="stop_at", type=int, help="stop working (and saving) at this age")
    s.add_argument("--auto", nargs="+", choices=["spending_monthly", "investing_yearly", "one_off_threshold",
                                                 "stop_work_age"],
                   help="back to the figure from history")
    s.add_argument("--one-off", dest="one_off", nargs="+", metavar="ID", help="leave these payments out of spending")
    s.add_argument("--keep", nargs="+", metavar="ID", help="count these payments after all")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_pension)

    s = sub.add_parser("plan", help="Scenario planner / financial independence (r17)")
    s.add_argument("--stop-at", dest="stop_at", type=float, help="age to stop working")
    s.add_argument("--extra", type=float, help="extra saving per month")
    s.add_argument("--spend-change", dest="spend_change", type=float, help="percent, e.g. -10")
    s.add_argument("--return", dest="ret", type=float, help="expected return in percent")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_plan)

    s = sub.add_parser("notify", help="Push notifications to your phone via ntfy (r17)")
    s.add_argument("action", choices=["setup", "test", "preview", "send"])
    s.add_argument("--topic")
    s.add_argument("--url")
    s.add_argument("--token")
    s.add_argument("--detail", choices=["short", "full"])
    s.add_argument("--public-url", dest="public_url")
    s.set_defaults(func=cmd_notify)

    s = sub.add_parser("networth", help="Net worth per month (reconstructed) and the daily snapshots")
    s.add_argument("--months", type=int)
    s.add_argument("--rebuild", action="store_true")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_networth)

    s = sub.add_parser("label", help="Clear out 'Other': list unlabelled merchants, or label one: label <merchant> <category>")
    s.add_argument("merchant", nargs="?")
    s.add_argument("category", nargs="?")
    s.add_argument("--scope", choices=["personal", "business"])
    s.add_argument("--limit", type=int, default=30)
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(func=cmd_label)

    s = sub.add_parser("analysis", help="Allocation vs targets, rebalancing, dividend calendar, benchmark")
    s.add_argument("--target", action="append", metavar="DIM:BUCKET=PCT,...",
                   help="e.g. class:etf=80,crypto=10,bond=10")
    s.add_argument("--new-money", type=float)
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_analysis)

    s = sub.add_parser("digest", help="Weekly digest (data folder/digest.html); --notify shows a Windows notification")
    s.add_argument("--notify", action="store_true")
    s.add_argument("--open", action="store_true")
    s.set_defaults(func=cmd_digest)

    s = sub.add_parser("schedule", help="Windows Task Scheduler jobs: sync twice a day, prices hourly, weekly digest")
    s.add_argument("action", choices=["install", "remove", "show"])
    s.add_argument("--dry-run", action="store_true", help="Only print the schtasks commands")
    s.set_defaults(func=cmd_schedule)

    s = sub.add_parser("loan", help="Home and loans: show, or set a loan's repayment plan from its statement")
    s.add_argument("account", nargs="?")
    s.add_argument("--outstanding", help="amount still owed on --statement-date")
    s.add_argument("--maturity", help="date of the last instalment (YYYY-MM-DD)")
    s.add_argument("--rate", help="periodic monthly rate in %, e.g. 0,14")
    s.add_argument("--principal")
    s.add_argument("--contract")
    s.add_argument("--statement-date")
    s.set_defaults(func=cmd_loan)

    s = sub.add_parser("app", help="Open 'centen bijwerken': a local web app to update balances, values and loans")
    s.add_argument("--port", type=int, default=8770)
    s.add_argument("--no-open", action="store_true")
    s.set_defaults(func=cmd_app)

    s = sub.add_parser("web", help="Run centen as a server: dashboard, update app, MCP over HTTP and the schedule")
    s.add_argument("--host", default=os.environ.get("CENTEN_HOST", "0.0.0.0"))
    s.add_argument("--port", type=int, default=int(os.environ.get("CENTEN_PORT", "8770")))
    s.add_argument("--no-scheduler", action="store_true", help="Don't run sync / prices / digest on a schedule")
    s.add_argument("--show-key", action="store_true", help="Print the access key and exit")
    s.set_defaults(func=cmd_web)

    s = sub.add_parser("remote", help="Connect Claude Desktop to a centen server elsewhere (stdio relay)")
    s.add_argument("url", help="e.g. http://nas.local:8770/mcp")
    s.add_argument("--key", help="access key (default: env CENTEN_KEY)")
    s.set_defaults(func=cmd_remote)

    s = sub.add_parser("nas-export", help="Prepare a folder to run centen on a NAS/home server (Docker)")
    s.add_argument("folder", help="e.g. \\\\NAS\\docker\\centen or a local folder you copy later")
    s.set_defaults(func=cmd_nas_export)

    s = sub.add_parser("backup", help="Consistent copy of the database (file or folder)")
    s.add_argument("path")
    s.set_defaults(func=cmd_backup)

    sub.add_parser("status", help="Overview as JSON").set_defaults(func=cmd_status)
    sub.add_parser("serve", help="Run the MCP server on stdio (used by Claude)").set_defaults(func=cmd_serve)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.db and args.cmd != "demo":
        os.environ["CENTEN_DB"] = str(Path(args.db).expanduser())
    args.func(args)


if __name__ == "__main__":
    main()
