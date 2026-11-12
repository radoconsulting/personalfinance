import json
import re

from centen import dashboard

from test_business import biz  # noqa: F401  (fixture)

TODAY_ISO = "2026-11-06"


def _payload(html: str) -> dict:
    m = re.search(r'<script type="application/json" id="centen-data">(.*?)</script>', html, re.S)
    assert m, "data block missing"
    return json.loads(m.group(1))


def test_collect_demo_shape(demo_conn):
    from datetime import date
    d = dashboard.collect(demo_conn, date.fromisoformat(TODAY_ISO))
    assert d["meta"]["default_period"] == "2026"
    assert d["period_order"][0] == "2026" and d["period_order"][-1] == "12m"
    p = d["periods"]["2026"]
    assert {"personal", "business", "cash", "company_to_you", "household"} <= set(p)
    assert "accounts" not in p["cash"] and "household_flows" not in p
    # monthly series cover every selectable year
    first_year = min(int(k) for k in d["periods"] if k.isdigit())
    assert d["monthly"]["personal"][0]["month"] <= f"{first_year}-01"
    inv = d["investments"]
    assert inv["positions"] is None and inv["valuation"] is None
    assert inv["timeline"]["series"] and len(inv["timeline"]["months"]) == len(inv["timeline"]["series"][0]["values"])
    assert inv["timeline"]["series"][0]["final"] == inv["net_deposited_total"]


def test_render_is_self_contained_and_safe(demo_conn, tmp_path):
    from datetime import date
    p = dashboard.build(demo_conn, tmp_path / "d.html", date.fromisoformat(TODAY_ISO))
    html = p.read_text(encoding="utf-8")
    assert "/*__DATA__*/" not in html
    assert not re.search(r'<script[^>]+src=|<link[^>]+href=|https?://', html.split('id="centen-data"')[0])
    data = _payload(html)
    assert data["meta"]["today"] == TODAY_ISO
    # no full IBANs in the page
    assert not re.search(r"\bBE\d{14}\b", html)


def test_script_closing_tag_in_data_is_escaped(demo_conn, tmp_path):
    demo_conn.execute("UPDATE transactions SET merchant='</script><b>x' WHERE rowid=(SELECT MIN(rowid) FROM transactions "
                      "WHERE category='Groceries')")
    demo_conn.commit()
    from datetime import date
    html = dashboard.render(dashboard.collect(demo_conn, date.fromisoformat(TODAY_ISO)))
    _payload(html)  # still parses: the JSON can't end the script element early
    assert html.count("</script>") == 2


def test_business_periods(biz):  # noqa: F811
    from datetime import date
    d = dashboard.collect(biz, date.fromisoformat(TODAY_ISO))
    p = d["periods"][d["meta"]["default_period"]]
    assert p["business"]["revenue_incl_vat"] > 0
    assert any(a["scope"] == "business" for a in d["accounts"])
    assert d["monthly"]["business"]
    assert "flows" in p["household"]


def test_refresh_never_raises(demo_conn, monkeypatch):
    monkeypatch.setattr(dashboard, "build", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert dashboard.refresh(demo_conn) is None


def test_template_survives_sandboxed_preview():
    # Previews run the page in a sandboxed frame (origin "null") where history/localStorage calls throw: every use
    # must be guarded, or switching tabs breaks.
    src = dashboard.TEMPLATE.read_text(encoding="utf-8")
    for api in ("history.replaceState", "localStorage."):
        for line in (l for l in src.splitlines() if api in l):
            assert "try {" in line, line.strip()
