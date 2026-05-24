/* telemetrify — Web Push service worker.
 *
 * Listens for `push` events, shows a desktop notification, and on click
 * focuses or opens the URL we shipped in the payload. Scope is root (`/`)
 * via the `Service-Worker-Allowed: /` header from the FastAPI handler.
 *
 * Payload shape (server side, push_notify.notify):
 *   { "title": str, "body": str, "url": str }
 */

const NOTIF_ICON = "/static/icons/notify-192.png";
const DEFAULT_URL = "/dashboard";

self.addEventListener("install", (event) => {
  // Activate immediately on first install rather than waiting for old
  // tabs to close — single-user local-only app, no compatibility concerns.
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  // Take control of any uncontrolled clients (e.g. the dashboard tab that
  // just registered us).
  event.waitUntil(self.clients.claim());
});

self.addEventListener("push", (event) => {
  let data = {};
  if (event.data) {
    try {
      data = event.data.json();
    } catch (e) {
      // Fallback to plain text if upstream sent a non-JSON payload.
      data = { title: "telemetrify", body: event.data.text() };
    }
  }

  const title = data.title || "telemetrify";
  const options = {
    body: data.body || "",
    icon: NOTIF_ICON,
    badge: NOTIF_ICON,
    tag: data.tag || "telemetrify",
    data: { url: data.url || DEFAULT_URL },
  };

  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const targetUrl = (event.notification.data && event.notification.data.url) || DEFAULT_URL;

  event.waitUntil(
    (async () => {
      const allClients = await self.clients.matchAll({
        type: "window",
        includeUncontrolled: true,
      });
      // Try to focus an existing tab whose path matches the notification URL.
      for (const client of allClients) {
        try {
          const url = new URL(client.url);
          const target = new URL(targetUrl, self.location.origin);
          if (url.pathname === target.pathname && "focus" in client) {
            return client.focus();
          }
        } catch (e) {
          // ignore parse errors
        }
      }
      // Otherwise just focus any telemetrify tab; failing that, open one.
      for (const client of allClients) {
        try {
          if (new URL(client.url).origin === self.location.origin && "focus" in client) {
            await client.focus();
            if ("navigate" in client) {
              try { await client.navigate(targetUrl); } catch (e) { /* ignore */ }
            }
            return;
          }
        } catch (e) { /* ignore */ }
      }
      if (self.clients.openWindow) {
        return self.clients.openWindow(targetUrl);
      }
    })(),
  );
});
