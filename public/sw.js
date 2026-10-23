/* Push runs without an open page. Never cache accounts, APIs, checkout or payment data. */
const SHELL = "duo-shell-v1";
self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(SHELL).then((cache) => cache.addAll(["/offline.html", "/icons/icon-512.png"])));
  self.skipWaiting();
});
self.addEventListener("activate", (event) => {
  event.waitUntil(Promise.all([caches.keys().then((keys) => Promise.all(keys.filter((key) => key.startsWith("duo-shell-") && key !== SHELL).map((key) => caches.delete(key)))), self.clients.claim()]));
});
self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (url.origin !== self.location.origin || event.request.method !== "GET" || url.pathname.startsWith("/api/")) return;
  if (event.request.mode === "navigate") {
    event.respondWith(fetch(event.request).catch(() => caches.match("/offline.html")));
  }
});
self.addEventListener("push", (event) => {
  let payload;
  try { payload = event.data.json(); } catch { return; }
  if (!["stock", "payment", "test"].includes(payload.kind)) return;
  const path = payload.kind === "payment" ? "/?view=orders" : "/#/monitor";
  event.waitUntil(self.registration.showNotification(String(payload.title || "DUO 提醒").slice(0, 80), {
    body: String(payload.body || "").slice(0, 200), icon: "/icons/icon-512.png", badge: "/icons/icon-512.png",
    tag: String(payload.tag || "duo").slice(0, 100), renotify: true, requireInteraction: payload.kind === "payment", data: { path },
  }));
});
self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const path = event.notification.data?.path === "/?view=orders" ? "/?view=orders" : "/#/monitor";
  const target = new URL(path, self.location.origin).href;
  event.waitUntil(self.clients.matchAll({ type: "window", includeUncontrolled: true }).then(async (windows) => {
    const existing = windows.find((client) => new URL(client.url).origin === self.location.origin);
    if (existing) { await existing.navigate(target); return existing.focus(); }
    return self.clients.openWindow(target);
  }));
});
