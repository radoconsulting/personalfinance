"""Phone support for `centen web` (r17): a web app manifest, icons and a small service worker, so the dashboard can be
added to the home screen and opened full screen, and the last dashboard you saw stays readable when the NAS is out
of reach. Browsers only allow this over HTTPS (or localhost): put the Synology reverse proxy with a certificate, or
`tailscale serve`, in front of centen. Over plain http the pages still work in the phone's browser.

Nothing with data is cached except the pages you opened (dashboard, digest); logging out clears the cache.
"""
from __future__ import annotations

import struct
import zlib
from functools import lru_cache

THEME = "#2a78d6"

MANIFEST = {
    "name": "centen", "short_name": "centen", "description": "Je financieel overzicht",
    "start_url": "/?bron=app", "scope": "/", "display": "standalone", "background_color": "#f9f9f7",
    "theme_color": THEME, "lang": "nl-BE",
    "icons": [{"src": "/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
              {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
              {"src": "/icon-maskable-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"}],
    "shortcuts": [{"name": "Prognose", "url": "/#forecast"}, {"name": "Vaste kosten", "url": "/#fixed"},
                  {"name": "Weekoverzicht", "url": "/digest"}],
}

SERVICE_WORKER = r"""
// centen service worker: network first; the last copy of a page is kept for when the server is out of reach.
const CACHE = "centen-v1";
const PAGES = ["/", "/digest"];
self.addEventListener("install", e => self.skipWaiting());
self.addEventListener("activate", e => e.waitUntil(
  caches.keys().then(ks => Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k)))).then(() => self.clients.claim())));
self.addEventListener("fetch", e => {
  const u = new URL(e.request.url);
  if (e.request.method !== "GET" || u.origin !== location.origin) return;
  if (u.pathname.startsWith("/api") || u.pathname.startsWith("/mcp") || u.pathname.startsWith("/log")) return;
  const page = PAGES.includes(u.pathname);
  const asset = /^\/(icon-|apple-touch|manifest)/.test(u.pathname);
  if (!page && !asset) return;
  e.respondWith(fetch(e.request).then(r => {
    if (r.ok && !r.redirected) { const c = r.clone(); caches.open(CACHE).then(x => x.put(u.pathname, c)); }
    return r;
  }).catch(() => caches.match(u.pathname).then(r => r || new Response(
    "<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'><body style='font:16px system-ui;padding:24px'>" +
    "<h1>centen</h1><p>De server is niet bereikbaar en er is nog geen kopie op dit toestel. Ben je verbonden met je thuisnetwerk of VPN?</p>",
    {headers: {"Content-Type": "text/html; charset=utf-8"}}))));
});
self.addEventListener("message", e => { if (e.data === "logout") caches.delete(CACHE); });
"""

HEAD = f"""<link rel="manifest" href="/manifest.webmanifest">
<meta name="theme-color" content="{THEME}">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="default">
<meta name="apple-mobile-web-app-title" content="centen">
<link rel="apple-touch-icon" href="/apple-touch-icon.png">
<script>if ("serviceWorker" in navigator && window.isSecureContext) addEventListener("load", () =>
  navigator.serviceWorker.register("/sw.js").catch(() => {{}}));
addEventListener("DOMContentLoaded", () => {{ if (!navigator.onLine) {{ const b = document.createElement("div");
  b.textContent = "Offline: je ziet de laatst opgehaalde versie."; b.style.cssText = "position:sticky;top:0;z-index:20;" +
  "background:#fab219;color:#0b0b0b;padding:6px 12px;font:13px system-ui;text-align:center"; document.body.prepend(b); }} }});
</script>"""


def inject(html: str) -> str:
    """Add the manifest, icons and service worker registration to a page served by `centen web`."""
    return html.replace("</head>", HEAD + "</head>", 1) if "</head>" in html else HEAD + html


def _png(size: int, maskable: bool = False) -> bytes:
    """A "c" on a blue tile, drawn in pure Python (no image library needed)."""
    bg = (0x2a, 0x78, 0xd6)
    white = (255, 255, 255)
    c = (size - 1) / 2
    radius_tile = size * (0 if maskable else 0.22)
    outer, inner = size * (0.27 if maskable else 0.31), size * (0.17 if maskable else 0.20)
    rows = []
    for y in range(size):
        row = bytearray([0])
        for x in range(size):
            # rounded tile (transparent corners unless maskable)
            dx = max(radius_tile - x, x - (size - 1 - radius_tile), 0)
            dy = max(radius_tile - y, y - (size - 1 - radius_tile), 0)
            inside_tile = maskable or (dx * dx + dy * dy) <= radius_tile * radius_tile
            if not inside_tile:
                row += bytes((0, 0, 0, 0))
                continue
            d = ((x - c) ** 2 + (y - c) ** 2) ** 0.5
            gap = x > c and abs(y - c) < (x - c) * 0.75  # opening on the right: a "c"
            col = white if inner <= d <= outer and not gap else bg
            row += bytes((*col, 255))
        rows.append(bytes(row))
    raw = zlib.compress(b"".join(rows), 9)

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)

    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)) + \
        chunk(b"IDAT", raw) + chunk(b"IEND", b"")


ICONS = ("icon-192.png", "icon-512.png", "icon-maskable-512.png", "apple-touch-icon.png")


@lru_cache(maxsize=8)
def icon(name: str) -> bytes | None:
    return {"icon-192.png": lambda: _png(192), "icon-512.png": lambda: _png(512),
            "icon-maskable-512.png": lambda: _png(512, True), "apple-touch-icon.png": lambda: _png(180, True)}\
        .get(name, lambda: None)()
