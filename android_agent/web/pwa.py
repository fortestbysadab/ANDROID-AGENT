"""Making the console installable to the home screen.

The console is already a single self-contained page served from Termux.
Three small additions turn it into something Android will install: a web
app manifest, a service worker, and icons.

Why this rather than an APK: no sideloading, no "restricted setting"
dialog, no second app to keep in step with the agent, and no rebuild when
the console changes. The trade is that the agent still has to be running
in Termux for the window to show anything, which was already true.
"""

from __future__ import annotations

import json
from pathlib import Path

STATIC_DIR = Path(__file__).with_name("static")

#: Chrome will only offer installation when it can read a manifest naming
#: the app, declaring a standalone display mode, and pointing at a 192px
#: and a 512px icon. The maskable copy is separate because launchers crop
#: icons to their own shape and would otherwise clip the artwork.
MANIFEST: dict = {
    "name": "Android Agent",
    "short_name": "Agent",
    "description": "The console for your Android Agent.",
    "start_url": "/",
    "scope": "/",
    "display": "standalone",
    "orientation": "any",
    "background_color": "#171a26",
    "theme_color": "#171a26",
    "icons": [
        {"src": "/static/icon-192.png", "sizes": "192x192", "type": "image/png",
         "purpose": "any"},
        {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png",
         "purpose": "any"},
        {"src": "/static/icon-maskable-512.png", "sizes": "512x512",
         "type": "image/png", "purpose": "maskable"},
    ],
}

#: Deliberately minimal. A service worker with a fetch handler is what makes
#: the app installable, but caching anything from this console would be a
#: privacy problem: the transcript, the files listing and every API reply
#: are private to the owner and must never be written to disk by the
#: browser. So only the shell is cached, and only to show something other
#: than a browser error when Termux is not running.
SERVICE_WORKER = """\
const SHELL = 'android-agent-shell-v1';

self.addEventListener('install', (event) => {
  // Take over immediately so the first launch after install is not a
  // half-controlled page.
  event.waitUntil(caches.open(SHELL).then((cache) => cache.add('/')));
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((names) =>
      Promise.all(names.filter((name) => name !== SHELL).map((name) => caches.delete(name)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const request = event.request;
  // Never touch anything but a plain page load. API traffic carries the
  // session cookie and private content; it goes straight to the network
  // and is never stored.
  if (request.method !== 'GET' || request.mode !== 'navigate') {
    return;
  }
  event.respondWith(
    fetch(request)
      .then((response) => {
        const copy = response.clone();
        caches.open(SHELL).then((cache) => cache.put('/', copy));
        return response;
      })
      .catch(() =>
        caches.match('/').then(
          (cached) =>
            cached ||
            new Response(
              '<!doctype html><meta charset="utf-8">' +
                '<title>Android Agent</title>' +
                '<body style="font:16px system-ui;padding:2rem;' +
                'background:#171a26;color:#f7f8fc">' +
                '<h1>Agent is not running</h1>' +
                '<p>Start the agent in Termux, then pull down to reload.</p>',
              { headers: { 'Content-Type': 'text/html; charset=utf-8' } }
            )
        )
      )
  );
});
"""

#: Served once at startup rather than read per request.
MANIFEST_BYTES = json.dumps(MANIFEST, indent=2).encode()
SERVICE_WORKER_BYTES = SERVICE_WORKER.encode()

ICON_TYPES = {".png": "image/png", ".svg": "image/svg+xml"}


def read_static(name: str) -> tuple[bytes, str] | None:
    """Return a static asset, refusing anything that escapes the folder."""
    if not name or "/" in name or "\\" in name or name.startswith("."):
        return None
    candidate = (STATIC_DIR / name).resolve()
    try:
        candidate.relative_to(STATIC_DIR.resolve())
    except ValueError:
        return None
    if not candidate.is_file():
        return None
    content_type = ICON_TYPES.get(candidate.suffix.lower())
    if content_type is None:
        return None
    return candidate.read_bytes(), content_type
