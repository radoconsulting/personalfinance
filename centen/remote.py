"""`centen remote <url>`: connect Claude Desktop to a centen server elsewhere (a NAS) — a small stdio <-> HTTP relay.

Claude Desktop starts local MCP servers as a command that talks JSON-RPC over stdin/stdout; this command forwards
every message to the server's /mcp endpoint (with the access key) and writes the answers back. In claude_desktop_config:

  "centen": {"command": "C:\\\\centen\\\\.venv\\\\Scripts\\\\centen.exe",
             "args": ["remote", "http://nas.local:8770/mcp"], "env": {"CENTEN_KEY": "<your key>"}}
"""
from __future__ import annotations

import json
import os
import sys

import httpx


def _sse_messages(text: str) -> list[dict]:
    out, data = [], []
    for line in text.splitlines():
        if line.startswith("data:"):
            data.append(line[5:].strip())
        elif not line.strip() and data:
            out.append(json.loads("\n".join(data)))
            data = []
    if data:
        out.append(json.loads("\n".join(data)))
    return out


def relay(url: str, key: str, stdin=None, stdout=None, client: httpx.Client | None = None) -> None:
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    http = client or httpx.Client(timeout=httpx.Timeout(300.0, connect=10.0))
    headers = {"Authorization": f"Bearer {key}", "Accept": "application/json, text/event-stream",
               "Content-Type": "application/json"}
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        msgs = msg if isinstance(msg, list) else [msg]
        try:
            r = http.post(url, content=line.encode("utf-8"), headers=headers)
            if r.status_code == 202 or not r.content:
                continue
            if r.status_code >= 400:
                raise RuntimeError(f"centen server answered {r.status_code}: {r.text[:200]}")
            ctype = r.headers.get("content-type", "")
            answers = _sse_messages(r.text) if "event-stream" in ctype else [r.json()]
            for a in answers:
                for x in (a if isinstance(a, list) else [a]):
                    stdout.write(json.dumps(x, ensure_ascii=False) + "\n")
            stdout.flush()
        except Exception as e:  # noqa: BLE001 - answer every request, also when the server can't be reached
            for m in msgs:
                if isinstance(m, dict) and "id" in m and "method" in m:
                    stdout.write(json.dumps({"jsonrpc": "2.0", "id": m["id"], "error": {
                        "code": -32000, "message": f"centen server not reachable: {e}"}}) + "\n")
            stdout.flush()


def main(url: str, key: str | None = None) -> None:
    key = key or os.environ.get("CENTEN_KEY")
    if not key:
        sys.exit("Set CENTEN_KEY (the server's access key) or pass --key.")
    relay(url, key)
