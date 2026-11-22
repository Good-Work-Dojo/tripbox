// SPDX-License-Identifier: AGPL-3.0-only
// Additional terms: see NOTICE
// The login page's one script: it empties the store of saved copies of this base (every
// release's), so nothing a signed-out device kept opens without a connection. A sign-out empties
// it before it is sent; this empties it again, also after a sign-out that came from elsewhere.
(function () {
  "use strict";
  const base = document.documentElement.dataset.base || "";
  const pages = "tripbox" + base + ":pages:";
  try {
    if (!window.caches) return; // a page not over https nor on localhost keeps nothing
    caches.keys()
      .then((names) => Promise.all(names.filter((n) => n.startsWith(pages)).map((n) => caches.delete(n))))
      .catch(() => {});
  } catch (e) { /* no store to reach */ }
})();
