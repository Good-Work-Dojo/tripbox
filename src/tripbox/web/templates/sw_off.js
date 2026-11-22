// SPDX-License-Identifier: AGPL-3.0-only
// Additional terms: see NOTICE
// tripbox's service worker while saved copies are switched off (TRIPBOX_OFFLINE): it answers no
// request, removes every store of this base's (whatever the release) and unregisters itself, so
// a device that kept copies keeps none once it opens the page again.
"use strict";

const BASE = {{ base|tojson }};
const PREFIX = "tripbox" + BASE + ":";

self.addEventListener("install", () => self.skipWaiting());

self.addEventListener("activate", (event) => {
  event.waitUntil(
    (async () => {
      for (const name of await caches.keys()) {
        if (name.startsWith(PREFIX)) await caches.delete(name);
      }
      await self.registration.unregister();
    })()
  );
});
