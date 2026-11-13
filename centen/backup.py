"""Backups of the database (r22): a consistent copy every night, the last 7 days and 8 weeks kept.

Default folder: <data folder>/backups (on the NAS: ./data/backups, so Hyper Backup picks it up with the rest).
Change with config.json → backup {"folder": "...", "keep_daily": 7, "keep_weekly": 8}.
Only centen-YYYY-MM-DD.db files in that folder are ever deleted.
"""
from __future__ import annotations

import re
import sqlite3
from contextlib import closing
from datetime import date
from pathlib import Path

from . import config

NAME = re.compile(r"^centen-(\d{4}-\d{2}-\d{2})\.db$")


def settings() -> dict:
    b = config.load().get("backup") or {}
    return {"folder": b.get("folder") or str(config.home() / "backups"),
            "keep_daily": int(b.get("keep_daily", 7)), "keep_weekly": int(b.get("keep_weekly", 8))}


def copy(conn, out: Path) -> Path:
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".db.part")
    with closing(sqlite3.connect(tmp)) as dst:
        conn.backup(dst)  # consistent, also while the server is writing
    try:
        tmp.chmod(0o600)
    except OSError:
        pass
    tmp.replace(out)
    return out


def keep(dates: list[date], keep_daily: int, keep_weekly: int) -> set[date]:
    """The newest `keep_daily` days, plus the newest copy of each of the newest `keep_weekly` ISO weeks."""
    ds = sorted(set(dates), reverse=True)
    kept = set(ds[:keep_daily])
    weeks: dict[tuple[int, int], date] = {}
    for d in ds:
        weeks.setdefault(d.isocalendar()[:2], d)
    kept |= set(sorted(weeks.values(), reverse=True)[:keep_weekly])
    return kept


def run(conn, today: date | None = None, folder: str | Path | None = None) -> dict:
    s = settings()
    today = today or date.today()
    folder = Path(folder or s["folder"]).expanduser()
    out = copy(conn, folder / f"centen-{today.isoformat()}.db")
    files = {date.fromisoformat(m.group(1)): p for p in folder.iterdir() if (m := NAME.match(p.name))}
    kept = keep(list(files), s["keep_daily"], s["keep_weekly"])
    removed = []
    for d, p in files.items():
        if d not in kept:
            p.unlink(missing_ok=True)
            removed.append(p.name)
    return {"file": out.name, "folder": str(folder), "kept": len(kept), "removed": len(removed),
            "size_mb": round(out.stat().st_size / 1e6, 1)}
