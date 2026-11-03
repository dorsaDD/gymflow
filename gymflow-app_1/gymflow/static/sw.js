// GymFlow service worker — minimal "installable + offline fallback" support
// (Section 3.3.4 Usability / NFR-8). Registered from base.html at root scope
// (see gymflow/__init__.py: service_worker() serves this with
// Service-Worker-Allowed: / so its scope covers the whole app, not just
// /static/).
//
// Deliberately small: this is not an offline-first app (every page needs a
// live session + real data), so the only thing cached is the dedicated
// /offline fallback page itself, shown when a navigation fails with no
// network instead of the browser's own "no internet" error screen.
const CACHE_NAME = "gymflow-offline-v1";
const OFFLINE_URL = "/offline";

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.add(OFFLINE_URL))
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  if (event.request.mode !== "navigate") return; // only guard page navigations
  event.respondWith(
    fetch(event.request).catch(() =>
      caches.open(CACHE_NAME).then((cache) => cache.match(OFFLINE_URL))
    )
  );
});
