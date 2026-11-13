"""The server's schedule (r22): which jobs are due, run them, remember when they last ran.

Up to r21 a job ran only when the clock showed exactly its time. A sync running past 07:55, or a restart during that
minute, silently skipped the crypto update for the day. Now every job has slots (e.g. sync at 07:40 and 18:40); a slot
is due when it has passed, it hasn't run yet and it is not older than the job's catch-up window. After a redeploy or
a sleeping NAS the most recent missed slot runs once, older ones are not replayed.

Jobs run one after the other in the scheduler thread: SQLite has one writer anyway, and a late job is better than a
job fighting for the database. Each finished run is stored in `meta` as `job:<name>` (slot, finish time, ok, short
message); the nav bar and "Te doen" read that.
"""
from __future__ import annotations

import json
import logging
import threading
from contextlib import closing
from datetime import date, datetime, timedelta
from pathlib import Path

from . import config, db

log = logging.getLogger("centen.jobs")

DEFAULT_SCHEDULE = {"sync": ["07:40", "18:40"], "prices_from": 8, "prices_to": 22, "digest": "mon 08:20",
                    "alerts": "08:30", "crypto": "07:55", "backup": "03:30"}

# how long after its slot a missed job still runs
CATCH_UP = {"sync": timedelta(hours=6), "prices": timedelta(minutes=55), "crypto": timedelta(hours=12),
            "alerts": timedelta(hours=8), "digest": timedelta(hours=24), "backup": timedelta(hours=20)}

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def schedule_cfg() -> dict:
    try:
        return {**DEFAULT_SCHEDULE, **(config.load().get("schedule") or {})}
    except Exception:  # noqa: BLE001
        return dict(DEFAULT_SCHEDULE)


def _at(d: date, hm: str) -> datetime:
    h, m = hm.strip().split(":")
    return datetime(d.year, d.month, d.day, int(h), int(m))


def slots(name: str, d: date, s: dict) -> list[datetime]:
    """The times job `name` should run on day d."""
    if name == "sync":
        return [_at(d, t) for t in s.get("sync") or []]
    if name == "prices":
        hours = range(int(s.get("prices_from", 8)), int(s.get("prices_to", 22)) + 1)
        if d.weekday() >= 5:  # weekends: only crypto moves, twice a day is enough
            hours = [h for h in hours if h in (10, 18)]
        return [datetime(d.year, d.month, d.day, h, 5) for h in hours]
    if name == "digest":
        day, hm = ((s.get("digest") or "mon 08:20").split() + ["08:20"])[:2]
        return [_at(d, hm)] if DAYS[d.weekday()] == day[:3].lower() else []
    t = s.get(name)
    return [_at(d, t)] if t else []


def due(now: datetime, s: dict, last: dict[str, datetime | None]) -> list[tuple[str, datetime]]:
    """Jobs whose latest passed slot hasn't run yet and is within the catch-up window; oldest slot first."""
    out = []
    for name, window in CATCH_UP.items():
        passed = [t for d in (now.date() - timedelta(days=1), now.date()) for t in slots(name, d, s) if t <= now]
        if not passed:
            continue
        slot = max(passed)
        if now - slot > window:
            continue
        if last.get(name) is not None and last[name] >= slot:
            continue
        out.append((name, slot))
    return sorted(out, key=lambda x: x[1])


# --- state in meta --------------------------------------------------------------------------------------------

def _key(name: str) -> str:
    return f"job:{name}"


def status(conn) -> dict[str, dict]:
    out = {}
    for r in conn.execute("SELECT key, value FROM meta WHERE key LIKE 'job:%'"):
        try:
            out[r[0][4:]] = json.loads(r[1])
        except ValueError:
            pass
    return out


def last_slots(conn) -> dict[str, datetime | None]:
    return {k: datetime.fromisoformat(v["slot"]) if v.get("slot") else None for k, v in status(conn).items()}


def record(conn, name: str, slot: datetime, ok: bool, message: str) -> None:
    db.set_meta(conn, _key(name), json.dumps({"slot": slot.isoformat(timespec="minutes"),
                                              "finished": datetime.now().isoformat(timespec="seconds"),
                                              "ok": ok, "message": message[:300]}))
    conn.commit()


# --- the jobs ---------------------------------------------------------------------------------------------------

def _sync(c):
    import contextlib
    from . import enablebanking, history, prices
    cfg = config.load()
    if not cfg["enablebanking"].get("app_id"):
        return {"skipped": "no bank connection configured"}
    res = enablebanking.sync(c, cfg)
    with contextlib.suppress(Exception):
        prices.refresh(c, max_age_minutes=15)
    with contextlib.suppress(Exception):
        history.ensure_history(c)
    if isinstance(res, dict):
        out = {k: v for k, v in res.items() if k != "accounts"}
        out["new"] = sum(a.get("new_transactions") or 0 for a in res.get("accounts") or [])
        return out
    return res


def _prices(c):
    import contextlib
    from . import history, prices
    res = prices.refresh(c, max_age_minutes=0)
    with contextlib.suppress(Exception):
        history.ensure_history(c)
    return {"updated": res.get("updated"), "missing": len(res.get("missing") or [])}


def _crypto(c):
    from . import crypto_sync
    st = crypto_sync.status(c)
    if not st["bitvavo"]["configured"] and not st["wallets"]:
        return {"skipped": "no Bitvavo key or wallets"}
    return crypto_sync.sync(c)


def _digest(c):
    from . import digest, notify
    d, _ = digest.write(c)
    if notify.configured():
        notify.send_digest(c, d["headline"])
    return d["headline"]


def _alerts(c):
    from . import notify
    if not notify.configured():
        return {"skipped": "notifications not configured"}
    return notify.run_alerts(c)


def _backup(c):
    from . import backup
    return backup.run(c)


JOBS = {"sync": _sync, "prices": _prices, "crypto": _crypto, "digest": _digest, "alerts": _alerts, "backup": _backup}
REBUILDS_DASHBOARD = {"sync", "prices", "crypto"}


def _errors(res, path: str = "") -> list[str]:
    """Error messages anywhere in a job result: bank sync `errors`, crypto `{"bitvavo": {"error": ...}}`, per wallet."""
    out = []
    if isinstance(res, dict):
        for k, v in res.items():
            if k == "errors" and isinstance(v, list):
                out += [str(e) for e in v]
            elif k == "error" and v:
                out.append(f"{path}: {v}" if path else str(v))
            elif isinstance(v, (dict, list)):
                out += _errors(v, res.get("account") or k)
    elif isinstance(res, list):
        for v in res:
            out += _errors(v, path)
    return out


def _failed(name: str, res) -> str | None:
    """A job that reports errors (bank sync, crypto) counts as failed for the status bar."""
    errs = _errors(res)
    return "; ".join(errs)[:300] if errs else None


def run(name: str, slot: datetime | None = None, on_change=None) -> dict:
    """Run one job now and record the result. Never raises."""
    slot = slot or datetime.now().replace(second=0, microsecond=0)
    try:
        with closing(db.connect()) as c:
            res = JOBS[name](c)
            err = _failed(name, res)
            record(c, name, slot, err is None, err or json.dumps(res, default=str, ensure_ascii=False)[:300])
        log.info("%s: %s", name, json.dumps(res, default=str)[:300] if res is not None else "ok")
        if name in REBUILDS_DASHBOARD and on_change:
            on_change()
        return {"ok": err is None, "result": res}
    except Exception as e:  # noqa: BLE001 - a failing job never stops the server
        log.warning("%s failed: %s", name, e)
        try:
            with closing(db.connect()) as c:
                record(c, name, slot, False, str(e))
        except Exception:  # noqa: BLE001
            pass
        return {"ok": False, "error": str(e)}


def scheduler(stop: threading.Event, on_change=None, interval: float = 30, now=datetime.now) -> None:
    while True:
        try:
            with closing(db.connect()) as c:
                last = last_slots(c)
            for name, slot in due(now(), schedule_cfg(), last):
                if stop.is_set():
                    return
                run(name, slot, on_change)
        except Exception as e:  # noqa: BLE001
            log.warning("scheduler: %s", e)
        if stop.wait(interval):
            return
