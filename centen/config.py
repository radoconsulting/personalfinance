"""Paths and settings. Everything lives under ~/.centen unless CENTEN_HOME / CENTEN_DB say otherwise."""
from __future__ import annotations

import json
import os
from pathlib import Path

DEFAULTS = {
    "enablebanking": {
        "app_id": "",
        "private_key_path": "",
        # The redirect URL whitelisted for the Enable Banking application (they require https).
        # It does not need to load: after you approve at your bank, the browser shows a "can't connect"
        # page; copy the address from the address bar back into `centen link`. Because nothing listens
        # on 127.0.0.1, the one-time code never leaves your computer.
        "redirect_url": "https://127.0.0.1:8765/callback",
        "country": "BE",
        "api_base": "https://api.enablebanking.com",
    },
    # Day of the month you usually get paid. Leave null to let centen detect it from your salary.
    "payday_day": None,
    # People in your household whose own accounts aren't linked: transfers to/from them are moves within the
    # household (visible as "Household transfers"), not spending. {"An": ["An Peeters"]}
    "household_members": {},
    # Minimum hours between automatic bank syncs. PSD2 allows banks to cap unattended access
    # at 4 times per day per account, so keep this at 6 or more.
    "min_sync_hours": 6,
}


def home() -> Path:
    p = Path(os.environ.get("CENTEN_HOME") or Path.home() / ".centen").expanduser()
    p.mkdir(parents=True, exist_ok=True)
    try:
        p.chmod(0o700)
    except OSError:
        pass
    return p


def db_path() -> Path:
    env = os.environ.get("CENTEN_DB")
    return Path(env).expanduser() if env else home() / "centen.db"


def config_path() -> Path:
    return home() / "config.json"


def _merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in (override or {}).items():
        out[k] = _merge(base[k], v) if isinstance(v, dict) and isinstance(base.get(k), dict) else v
    return out


def load() -> dict:
    p = config_path()
    if p.exists():
        return _merge(DEFAULTS, json.loads(p.read_text(encoding="utf-8")))
    return json.loads(json.dumps(DEFAULTS))


def save(cfg: dict) -> Path:
    """Write config.json atomically and private from the first byte (it can hold the Bitvavo secret): a temporary
    file created with mode 0600 in the same folder, then renamed over the old one. A crash never leaves half a file."""
    p = config_path()
    data = json.dumps(cfg, indent=2).encode("utf-8")
    tmp = p.with_name(f".{p.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0), 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    try:
        p.chmod(0o600)
    except OSError:
        pass
    return p
