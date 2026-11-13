"""r22: the schedule catches up after a restart or a slow job, never replays old slots; backups rotate."""
import threading
from datetime import date, datetime, timedelta

from centen import backup, health, jobs, notify

S = jobs.DEFAULT_SCHEDULE


def test_due_runs_a_slot_that_was_missed_while_another_job_ran():
    # sync started 07:40 and took 20 minutes: crypto (07:55) is now late, not skipped
    now = datetime(2026, 11, 13, 8, 0)
    last = {"sync": datetime(2026, 11, 13, 7, 40), "prices": datetime(2026, 11, 13, 7, 5) - timedelta(days=1),
            "backup": datetime(2026, 11, 13, 3, 30), "digest": None, "alerts": datetime(2026, 11, 12, 8, 30)}
    names = [n for n, _ in jobs.due(now, S, last)]
    assert "crypto" in names and "sync" not in names and "backup" not in names


def test_due_after_a_restart_runs_the_latest_slot_once_and_skips_old_ones():
    now = datetime(2026, 11, 13, 20, 30)  # container was down from 07:00 until now
    last = {k: datetime(2026, 11, 13, 6, 0) for k in jobs.CATCH_UP}
    due = dict(jobs.due(now, S, last))
    assert due["sync"] == datetime(2026, 11, 13, 18, 40)  # the 07:40 slot isn't replayed
    assert due["prices"] == datetime(2026, 11, 13, 20, 5)
    assert "crypto" not in due  # 07:55 is more than 12 hours ago
    assert "alerts" not in due  # 08:30 + 8 h has passed
    last.update({k: v for k, v in due.items()})
    assert jobs.due(now, S, last) == []


def test_weekend_prices_and_weekly_digest_slots():
    sat = date(2026, 11, 14)
    assert [t.hour for t in jobs.slots("prices", sat, S)] == [10, 18]
    mon = date(2026, 11, 16)
    assert jobs.slots("digest", mon, S) == [datetime(2026, 11, 16, 8, 20)] and jobs.slots("digest", sat, S) == []


def test_scheduler_records_runs_in_meta(conn, monkeypatch):
    calls = []
    monkeypatch.setitem(jobs.JOBS, "backup", lambda c: calls.append("b") or {"file": "x"})
    for k in list(jobs.JOBS):
        if k != "backup":
            monkeypatch.setitem(jobs.JOBS, k, lambda c: {"skipped": "test"})
    stop = threading.Event()
    changed = []
    t = threading.Thread(target=jobs.scheduler, args=(stop, lambda: changed.append(1)),
                         kwargs={"interval": 0.05, "now": lambda: datetime(2026, 11, 13, 9, 0)})
    t.start()
    import time
    time.sleep(0.5)
    stop.set()
    t.join(2)
    assert calls == ["b"]  # once, not every loop
    st = jobs.status(conn)
    assert st["backup"]["ok"] and st["backup"]["slot"] == "2026-11-13T03:30" and changed


def test_job_errors_count_as_failures(conn, monkeypatch):
    monkeypatch.setitem(jobs.JOBS, "crypto", lambda c: {"bitvavo": {"error": "403 key"}, "wallets": {"wallets": []}})
    r = jobs.run("crypto", datetime(2026, 11, 13, 7, 55))
    assert not r["ok"] and "403 key" in jobs.status(conn)["crypto"]["message"]


def test_backup_rotation(conn, tmp_path):
    folder = tmp_path / "b"
    folder.mkdir()
    d0 = date(2026, 8, 1)
    for i in range(100):
        (folder / f"centen-{(d0 + timedelta(days=i)).isoformat()}.db").write_bytes(b"x")
    (folder / "notes.txt").write_text("keep me")
    res = backup.run(conn, today=d0 + timedelta(days=100), folder=folder)
    left = sorted(p.name for p in folder.iterdir())
    assert "notes.txt" in left and res["file"] in left
    dbs = [p for p in left if p.endswith(".db")]
    assert len(dbs) <= 7 + 8 and len(dbs) == res["kept"]
    import sqlite3
    assert sqlite3.connect(folder / res["file"]).execute("SELECT COUNT(*) FROM accounts").fetchone()


def _link(conn, bank, valid_until, status="active", lid=None):
    lid = lid or f"l-{bank}"
    conn.execute("INSERT INTO bank_links(id, aspsp_name, country, session_id, valid_until, status) VALUES (?,?,?,?,?,?)",
                 (lid, bank, "BE", "s", valid_until, status))
    conn.execute("INSERT INTO accounts(id, source, bank, name, link_id) VALUES (?,?,?,?,?)",
                 (f"a-{lid}", "enablebanking", bank, f"{bank} zicht", lid))
    conn.commit()


def test_health_and_consent_notifications(conn):
    today = date(2026, 11, 13)
    _link(conn, "KBC", "2026-11-25T10:00:00+00:00")       # 12 days
    _link(conn, "Belfius", "2026-11-15T10:00:00+00:00")   # 2 days
    _link(conn, "Crelan", "2027-04-01T10:00:00+00:00")
    conn.execute("INSERT INTO sync_log(account_id, started_at, finished_at, status, message, n_new) VALUES "
                 "('a-l-Crelan','2026-11-13T06:40:00+00:00','2026-11-13T06:41:00+00:00','error','401 Unauthorized',0)")
    conn.commit()
    h = health.summary(conn, today, datetime(2026, 11, 13, 9, 0))
    kinds = [(i["kind"], i.get("bank"), i["severity"]) for i in h["issues"]]
    assert ("consent", "Belfius", "critical") in kinds and ("consent", "KBC", "warning") in kinds
    assert ("sync_error", "Crelan", "warning") in kinds and h["feeds"]["bank"]["state"] == "error"
    ids = [i["id"] for i in notify.collect(conn, today) if i["kind"] in ("consent", "sync_error")]
    assert any(i.startswith("consent|KBC|2026-11-25|14") for i in ids)
    assert any(i.startswith("consent|Belfius|2026-11-15|3") for i in ids)
    assert any(i.startswith("sync_error|Crelan") for i in ids)


def test_relinking_replaces_the_old_link(conn):
    from centen import db
    _link(conn, "KBC", "2026-11-20T00:00:00+00:00", lid="old")
    conn.execute("INSERT INTO bank_links(id, aspsp_name, country, session_id, valid_until, status, created_at) "
                 "VALUES ('new','KBC','BE','s2','2027-05-10T00:00:00+00:00','active', datetime('now','+1 second'))")
    conn.execute("UPDATE accounts SET link_id='new' WHERE link_id='old'")
    db._replaced_links(conn)
    st = dict(conn.execute("SELECT id, status FROM bank_links").fetchall())
    assert st == {"old": "replaced", "new": "active"}
    assert [l["id"] for l in health.links(conn, date(2026, 11, 13))] == ["new"]


def test_tax_payments_are_never_a_price_increase():
    from centen import analytics, demo
    from centen.db import connect
    import tempfile, os
    with tempfile.TemporaryDirectory() as d:
        c = connect(os.path.join(d, "x.db"))
        demo.generate_full(c, today=date(2026, 11, 13))
        alerts = analytics.unusual(c, 120, date(2026, 11, 13))
        inc = [a for a in alerts if a["type"] == "price_increase"]
        assert inc and not [a for a in inc if a["category"] in analytics.AMOUNT_VARIES]
        c.close()


def test_connect_sets_up_once_and_then_writes_nothing(tmp_path, monkeypatch):
    from centen import db
    monkeypatch.setenv("CENTEN_HOME", str(tmp_path))
    p = tmp_path / "v.db"
    c = db.connect(p)
    assert c.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    c.close()
    c = db.connect(p)
    assert c.total_changes == 0  # same version: no schema, category or meta writes on every connection
    c.execute("PRAGMA user_version=21")  # an older database is brought up to date
    c.commit()
    c.close()
    c = db.connect(p)
    assert c.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION and c.total_changes > 0
    c.close()


def test_config_is_written_atomically_and_private(tmp_path, monkeypatch):
    import os, stat
    from centen import config
    monkeypatch.setenv("CENTEN_HOME", str(tmp_path))
    cfg = config.load()
    cfg["bitvavo"] = {"key": "k", "secret": "s"}
    p = config.save(cfg)
    assert config.load()["bitvavo"]["secret"] == "s" and not list(tmp_path.glob(".config.json.*"))
    if os.name == "posix":
        assert stat.S_IMODE(p.stat().st_mode) == 0o600


def test_crypto_endpoints_can_point_to_your_own_node(tmp_path, monkeypatch):
    from centen import config, crypto_sync
    monkeypatch.setenv("CENTEN_HOME", str(tmp_path))
    assert crypto_sync.endpoints()["btc"] == "https://mempool.space/api"
    cfg = config.load()
    cfg["crypto_endpoints"] = {"btc": "http://nas.local:3006/api/", "eth": "http://nas.local:8545"}
    config.save(cfg)
    assert crypto_sync.endpoints() == {"btc": "http://nas.local:3006/api", "eth": "http://nas.local:8545"}
