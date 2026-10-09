const CACHE_NAME = "weekly-planner-shell-v1";
const APP_SHELL = ["/", "/static/style.css", "/static/app.js"];

self.addEventListener("install", event => {
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then(cache => cache.addAll(APP_SHELL))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", event => {
  event.waitUntil(
    caches.keys()
      .then(keys => Promise.all(
        keys.filter(key => key.startsWith("weekly-planner-shell-") && key !== CACHE_NAME)
          .map(key => caches.delete(key))
      ))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", event => {
  const request = event.request;
  const url = new URL(request.url);
  if (request.method !== "GET" || url.origin !== self.location.origin) return;

  // Planner data must always be requested from the server. Pending edits and
  // the last displayed state are stored locally by the app itself.
  if (url.pathname.startsWith("/api/")) return;

  const isAppShell = url.pathname === "/"
    || url.pathname === "/static/style.css"
    || url.pathname === "/static/app.js";
  if (!isAppShell) return;

  event.respondWith((async () => {
    try {
      const response = await fetch(request);
      if (response.ok) {
        const cache = await caches.open(CACHE_NAME);
        await cache.put(request, response.clone());
      }
      return response;
    } catch (error) {
      const cached = await caches.match(request, { ignoreVary: true });
      if (cached) return cached;
      return new Response("Weekly Planner is offline. Reconnect to load the app.", {
        status: 503,
        headers: { "Content-Type": "text/plain; charset=utf-8" },
      });
    }
  })());
});
