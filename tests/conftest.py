from datetime import date

import pytest

from centen import db, demo

TODAY = date(2026, 11, 6)


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("CENTEN_HOME", str(tmp_path / "home"))
    path = tmp_path / "t.db"
    monkeypatch.setenv("CENTEN_DB", str(path))
    c = db.connect(path)
    yield c
    c.close()


@pytest.fixture()
def demo_conn(conn):
    demo.generate(conn, today=TODAY)
    return conn
