"""Windows Task Scheduler jobs for centen, created with schtasks.exe (no admin rights needed; they run as you, only
while you're logged on, so nothing runs with stored passwords):

  centen-sync      every day at 07:40 and 18:40   centen sync        (banks allow about 4 unattended syncs a day)
  centen-prices    every hour 08:05-22:05         centen prices      (live prices + price history, dashboard)
  centen-digest    Monday 08:20                   centen digest --notify

`centen schedule install` creates (or replaces) them, `centen schedule remove` deletes them, `centen schedule show`
lists them. Each job calls centen.cmd in the install folder, so it uses the same data folder as Claude Desktop.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

PREFIX = "centen-"


def _cmd_path() -> Path:
    """centen.cmd next to the virtualenv: <install>\\.venv\\Scripts\\centen.exe -> <install>\\centen.cmd."""
    exe = Path(sys.executable).resolve()
    for p in [exe.parent.parent.parent, exe.parent.parent, Path.cwd()]:
        if (p / "centen.cmd").exists():
            return p / "centen.cmd"
    raise FileNotFoundError("centen.cmd not found next to the virtualenv; run this from the centen install folder.")


_VBS = """' Runs centen.cmd without a console window popping up (used by the scheduled tasks).
Dim sh, args, i
Set sh = CreateObject("WScript.Shell")
args = ""
For i = 0 To WScript.Arguments.Count - 1
  args = args & " " & WScript.Arguments(i)
Next
sh.Run Chr(34) & "{cmd}" & Chr(34) & args, 0, True
"""


def launcher(cmd: Path) -> Path:
    """centen-hidden.vbs next to centen.cmd."""
    p = cmd.with_name("centen-hidden.vbs")
    p.write_text(_VBS.replace("{cmd}", str(cmd)), encoding="utf-8")
    return p


def jobs(cmd: Path) -> list[dict]:
    q = f'wscript.exe "{cmd.with_name("centen-hidden.vbs")}"'
    return [
        {"name": PREFIX + "sync-morning", "args": ["/SC", "DAILY", "/ST", "07:40"], "run": f"{q} sync",
         "what": "Banken synchroniseren (ochtend)"},
        {"name": PREFIX + "sync-evening", "args": ["/SC", "DAILY", "/ST", "18:40"], "run": f"{q} sync",
         "what": "Banken synchroniseren (avond)"},
        {"name": PREFIX + "prices", "args": ["/SC", "HOURLY", "/MO", "1", "/ST", "08:05", "/DU", "0014:00"],
         "run": f"{q} prices --quiet", "what": "Koersen en dashboard elk uur (08-22u)"},
        {"name": PREFIX + "digest", "args": ["/SC", "WEEKLY", "/D", "MON", "/ST", "08:20"],
         "run": f"{q} digest --notify", "what": "Weekoverzicht met melding (maandag)"},
    ]


def _schtasks(args: list[str]) -> tuple[int, str]:
    if sys.platform != "win32":
        return 1, "Task Scheduler is only on Windows."
    r = subprocess.run(["schtasks.exe", *args], capture_output=True, text=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return r.returncode, (r.stdout or r.stderr or "").strip()


def install(dry_run: bool = False) -> list[dict]:
    cmd = _cmd_path()
    if not dry_run:
        launcher(cmd)
    out = []
    for j in jobs(cmd):
        args = ["/Create", "/F", "/TN", j["name"], "/TR", j["run"], *j["args"]]
        if dry_run:
            out.append({"name": j["name"], "what": j["what"], "command": "schtasks " + " ".join(args)})
            continue
        code, msg = _schtasks(args)
        out.append({"name": j["name"], "what": j["what"], "ok": code == 0, "message": msg})
    return out


def remove() -> list[dict]:
    out = []
    for j in jobs(Path("centen.cmd")):
        code, msg = _schtasks(["/Delete", "/F", "/TN", j["name"]])
        out.append({"name": j["name"], "ok": code == 0, "message": msg})
    return out


def show() -> list[dict]:
    out = []
    for j in jobs(Path("centen.cmd")):
        code, msg = _schtasks(["/Query", "/TN", j["name"], "/FO", "LIST"])
        out.append({"name": j["name"], "installed": code == 0, "details": msg if code == 0 else None})
    return out
