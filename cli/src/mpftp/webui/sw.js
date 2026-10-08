// Offline app shell only — the WebSocket board session obviously needs the
// network; this just lets the page itself (and a "not connected" state)
// load when the network is briefly down or the tab is reopened offline.
const CACHE = "mpftp-shell-v2";
const SHELL = [
  "/",
  "/app.js",
  "/app.css",
  "/ftp.js",
  "/ftp.css",
  "/codicons/codicon.css",
  "/codicons/codicon.ttf",
  "/manifest.webmanifest",
  "/icons/icon.svg",
  "/icons/icon-192.png",
  "/icons/icon-512.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE).then((cache) => cache.addAll(SHELL)).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

// Network first: the page and its server come from the same mpftp install, so
// a fresh copy is always right after an upgrade; the cache only covers the
// server being down (the page still opens and says it can't reach mpftp).
self.addEventListener("fetch", (event) => {
  if (event.request.method !== "GET" || event.request.url.startsWith("ws")) {
    return;
  }
  event.respondWith(
    fetch(event.request)
      .then((res) => {
        if (res.ok) {
          const copy = res.clone();
          caches.open(CACHE).then((cache) => cache.put(event.request, copy));
        }
        return res;
      })
      .catch(() => caches.match(event.request).then((cached) => cached || Response.error()))
  );
});
