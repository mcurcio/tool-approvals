// The desk's push page: register this browser for push notices of Approvals waiting for the signed-in approver,
// or stop them. Everything it needs is in the #push section's data attributes; it never reads agent text.
"use strict";

(() => {
  const section = document.getElementById("push");
  if (!section || !("serviceWorker" in navigator) || !("PushManager" in window) || !("Notification" in window)) {
    return;
  }
  const on = document.getElementById("push-on");
  const off = document.getElementById("push-off");
  const status = document.getElementById("push-status");
  const settings = section.dataset;

  const fromBase64Url = (text) => {
    const padded = text.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - (text.length % 4)) % 4);
    return Uint8Array.from(atob(padded), (char) => char.charCodeAt(0));
  };
  const toBase64Url = (buffer) =>
    btoa(String.fromCharCode(...new Uint8Array(buffer)))
      .replace(/\+/g, "-")
      .replace(/\//g, "_")
      .replace(/=+$/, "");

  const send = (method, body) =>
    fetch(settings.subscriptions, {
      method,
      credentials: "same-origin",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ ...body, form_token: settings.pushToken }),
    });

  const show = (subscribed) => {
    on.hidden = subscribed;
    off.hidden = !subscribed;
  };

  // The worker's scope is the push page's own directory, never the reviews or the list (it controls none of them).
  const scope = settings.serviceWorkerScope;

  const subscription = async () => {
    const registration = await navigator.serviceWorker.getRegistration(scope);
    return registration ? registration.pushManager.getSubscription() : null;
  };

  // The push page lies outside the scope, so navigator.serviceWorker.ready never settles here: wait for the
  // registration's own worker to activate.
  const activated = (registration) =>
    registration.active
      ? Promise.resolve(registration)
      : new Promise((resolve, reject) => {
          const worker = registration.installing || registration.waiting;
          worker.addEventListener("statechange", () => {
            if (worker.state === "activated") {
              resolve(registration);
            } else if (worker.state === "redundant") {
              reject(new Error("the service worker was not installed"));
            }
          });
        });

  // Register (or renew) this browser's subscription with the desk; a subscription routes notices only for a while
  // after its last registration, so the page sends it again on each visit.
  const keep = (pushed) =>
    send("POST", {
      endpoint: pushed.endpoint,
      keys: { p256dh: toBase64Url(pushed.getKey("p256dh")), auth: toBase64Url(pushed.getKey("auth")) },
    });

  on.addEventListener("click", async () => {
    try {
      if ((await Notification.requestPermission()) !== "granted") {
        status.textContent = "Notifications are blocked for this site in this browser.";
        return;
      }
      const registration = await activated(await navigator.serviceWorker.register(settings.serviceWorker, { scope }));
      const pushed =
        (await registration.pushManager.getSubscription()) ||
        (await registration.pushManager.subscribe({
          userVisibleOnly: true,
          applicationServerKey: fromBase64Url(settings.applicationServerKey),
        }));
      const response = await keep(pushed);
      if (response.ok) {
        show(true);
        status.textContent = "This browser will be notified.";
      } else {
        status.textContent = `This browser was not registered (HTTP ${response.status}).`;
      }
    } catch {
      status.textContent = "This browser could not be registered.";
    }
  });

  off.addEventListener("click", async () => {
    try {
      const pushed = await subscription();
      if (pushed) {
        await send("DELETE", { endpoint: pushed.endpoint });
        await pushed.unsubscribe();
      }
      show(false);
      status.textContent = "This browser will not be notified.";
    } catch {
      status.textContent = "This browser could not be removed.";
    }
  });

  subscription().then(
    async (pushed) => {
      show(Boolean(pushed));
      if (pushed && Notification.permission === "granted") {
        const response = await keep(pushed); // renews it: an approver who visits keeps getting notices
        if (!response.ok) {
          status.textContent = `This browser's notices were not renewed (HTTP ${response.status}).`;
        }
      }
    },
    () => show(false),
  ).catch(() => {
    status.textContent = "This browser's notices could not be renewed.";
  });
})();
