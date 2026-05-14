/* prompt-telemetry — Web Push page-side client.
 *
 * Exposes `window.promptTelemetryPush` with `subscribe()`, `unsubscribe()`,
 * and `status()`. The dashboard's toggle calls into these directly.
 *
 * NOTE on HTTPS: Web Push normally requires a secure origin (HTTPS), but
 * Chrome / Firefox / Edge / Safari all treat `http://127.0.0.1` and
 * `http://localhost` as secure for Service-Worker + Push purposes. This
 * file therefore Just Works against the local uvicorn at 127.0.0.1:8767.
 * If you ever expose the dashboard over LAN under plain HTTP, push will
 * stop registering — that's a browser policy, not a bug here.
 */

(function () {
  "use strict";

  const SW_URL = "/sw.js";              // root-scoped (see app.py route)
  const VAPID_KEY_URL = "/api/push/vapid-public-key";
  const SUBSCRIBE_URL = "/api/push/subscribe";
  const UNSUBSCRIBE_URL = "/api/push/unsubscribe";

  // ── helpers ────────────────────────────────────────────────────────

  function urlBase64ToUint8Array(base64String) {
    // The browser's pushManager.subscribe wants a Uint8Array of the raw
    // public key bytes; the server hands us URL-safe base64 (no padding).
    const padding = "=".repeat((4 - (base64String.length % 4)) % 4);
    const base64 = (base64String + padding).replace(/-/g, "+").replace(/_/g, "/");
    const rawData = atob(base64);
    const buf = new Uint8Array(rawData.length);
    for (let i = 0; i < rawData.length; i++) buf[i] = rawData.charCodeAt(i);
    return buf;
  }

  function supported() {
    return (
      typeof window !== "undefined" &&
      "serviceWorker" in navigator &&
      "PushManager" in window &&
      "Notification" in window
    );
  }

  async function fetchVapidKey() {
    const r = await fetch(VAPID_KEY_URL, { credentials: "same-origin" });
    if (!r.ok) throw new Error("vapid key fetch failed: " + r.status);
    const json = await r.json();
    if (!json.key) throw new Error("server returned empty vapid key");
    return json.key;
  }

  async function registerSW() {
    return await navigator.serviceWorker.register(SW_URL, { scope: "/" });
  }

  function subscriptionAsJSON(subscription) {
    // Some older browsers don't return keys via .toJSON() reliably; build
    // the object by hand so server-side parsing is always consistent.
    const raw = subscription.toJSON();
    return {
      endpoint: subscription.endpoint,
      keys: {
        p256dh: raw.keys && raw.keys.p256dh,
        auth: raw.keys && raw.keys.auth,
      },
    };
  }

  // ── public API ─────────────────────────────────────────────────────

  async function status() {
    if (!supported()) {
      return { supported: false, permission: "denied", subscribed: false };
    }
    let subscribed = false;
    try {
      const reg = await navigator.serviceWorker.getRegistration(SW_URL);
      if (reg) {
        const sub = await reg.pushManager.getSubscription();
        subscribed = !!sub;
      }
    } catch (e) { /* ignore */ }
    return {
      supported: true,
      permission: Notification.permission,
      subscribed,
    };
  }

  async function subscribe() {
    if (!supported()) {
      throw new Error("Web Push is not supported in this browser.");
    }
    const perm = await Notification.requestPermission();
    if (perm !== "granted") {
      throw new Error("notification permission " + perm);
    }
    const vapidB64 = await fetchVapidKey();
    const reg = await registerSW();
    // Wait until the SW is active before subscribing — pushManager can
    // get cranky if it's still installing.
    if (!reg.active) {
      await new Promise((resolve) => {
        const sw = reg.installing || reg.waiting;
        if (!sw) return resolve();
        sw.addEventListener("statechange", () => {
          if (sw.state === "activated") resolve();
        });
      });
    }
    const subscription = await reg.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: urlBase64ToUint8Array(vapidB64),
    });
    const body = {
      subscription: subscriptionAsJSON(subscription),
      user_agent: navigator.userAgent,
    };
    const r = await fetch(SUBSCRIBE_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "same-origin",
      body: JSON.stringify(body),
    });
    if (!r.ok) throw new Error("subscribe POST failed: " + r.status);
    return await r.json();
  }

  async function unsubscribe() {
    if (!supported()) return { removed: false };
    const reg = await navigator.serviceWorker.getRegistration(SW_URL);
    if (!reg) return { removed: false };
    const sub = await reg.pushManager.getSubscription();
    if (!sub) return { removed: false };
    const endpoint = sub.endpoint;
    let ok = false;
    try { ok = await sub.unsubscribe(); } catch (e) { ok = false; }
    try {
      await fetch(UNSUBSCRIBE_URL, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin",
        body: JSON.stringify({ endpoint }),
      });
    } catch (e) { /* best-effort */ }
    return { removed: ok };
  }

  window.promptTelemetryPush = {
    subscribe,
    unsubscribe,
    status,
    urlBase64ToUint8Array, // exposed for tests / debugging
  };
})();
