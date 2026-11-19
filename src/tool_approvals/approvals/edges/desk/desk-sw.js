// The desk's service worker: shows a notice for approval.waiting, replaces it (same tag: the ticket) for
// approval.resolved, and opens the Approval's review on a click. A link that is not this desk's own is never
// opened: the desk's list is opened instead.
"use strict";

const deskLink = (link) => {
  const fallback = new URL("approvals", self.registration.scope).href;
  try {
    const url = new URL(String(link || ""), self.registration.scope);
    return url.origin === self.location.origin ? url.href : fallback;
  } catch {
    return fallback;
  }
};

self.addEventListener("push", (event) => {
  let notice;
  try {
    notice = event.data ? event.data.json() : {};
  } catch {
    notice = {};
  }
  const data = notice && typeof notice.data === "object" && notice.data ? notice.data : {};
  const ticket = String(data.ticket || "");
  const link = deskLink(data.deskLink || data.desk_link);
  const resolved = notice.type === "approval.resolved";
  const title = resolved ? "No longer waiting" : "An approval is waiting";
  const body = [data.principal, data.server, data.tool].filter((part) => part).map(String).join(" · ");
  event.waitUntil(
    self.registration.showNotification(title, {
      body,
      tag: ticket || undefined,
      renotify: false,
      data: { link },
    }),
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  event.waitUntil(self.clients.openWindow(deskLink(event.notification.data && event.notification.data.link)));
});
