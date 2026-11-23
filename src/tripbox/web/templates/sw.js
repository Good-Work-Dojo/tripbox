// SPDX-License-Identifier: AGPL-3.0-only
// Additional terms: see NOTICE
// tripbox's service worker: saved copies of the trip list and of the trips opened, so they open
// with no network, and nothing else. It keeps only a page the server marked (X-Tripbox-Offline:
// 1, a signed-in 200 of the list or a trip), never by its path alone. Registered from
// <base>/sw.js with the scope <base>/; the stores are named for the base and the release, so a
// new release replaces them.
"use strict";

const BASE = {{ base|tojson }};
const VERSION = {{ version|tojson }};
const PREFIX = "tripbox" + BASE + ":";
const STATIC = PREFIX + "static:" + VERSION;
const PAGES = PREFIX + "pages:" + VERSION;
const KEEP = "X-Tripbox-Offline";
const SAVED = "X-Tripbox-Saved";
const WAIT_MS = 3000; // how long a page waits for the network before its saved copy
const MOST = 60; // pages kept; the least recently stored goes first
const MAX_AGE_MS = 30 * 24 * 60 * 60 * 1000; // a store whose newest copy is older is emptied
const NO_COPY =
  "You are offline and this page has no saved copy. Pages you open while connected are kept for next time.";
const NOT_SAVED =
  "You are offline, so this was not saved. Go back and try again when you are connected.";
// Never answered here: the sign-in, a shared trip, the feed, this script and the manifest
const UNTOUCHED = ["/login", "/logout", "/sw.js", "/manifest.webmanifest"];
const UNTOUCHED_UNDER = ["/s/", "/feed/"];
const TRIP = /^\/trips\/[0-9]+$/;
// What every saved copy needs to show as itself (its stylesheet, its script and its font), at
// the addresses the pages ask for them, written from the base. The page that registers this
// worker loads them before the worker answers anything, so they are kept here at install: a
// page kept then (the list sign-in lands on) opens offline whole, its banner and locks included.
const FILES = {{ files|tojson }};

self.addEventListener("install", (event) => {
  self.skipWaiting();
  const kept = (store, f) => store.add(BASE + f).catch(() => null);
  event.waitUntil(
    caches
      .open(STATIC)
      .then((store) => Promise.all(FILES.map((f) => kept(store, f))))
      .catch(() => null)
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    (async () => {
      for (const name of await caches.keys()) {
        if (name.startsWith(PREFIX) && name !== STATIC && name !== PAGES) await caches.delete(name);
      }
      await self.clients.claim();
    })()
  );
});

// The request's path written from the base ("/", "/trips/7"), or null for one this worker leaves
// alone: another origin's, or one outside <base>/.
function local(url) {
  if (url.origin !== self.location.origin || !url.pathname.startsWith(BASE + "/")) return null;
  return url.pathname.slice(BASE.length);
}

// A page of its own (the policy of the page's, which admits its own files alone), status 503.
function said(sentence) {
  const html =
    '<!doctype html><html lang="en" data-base="' + BASE + '"><head><meta charset="utf-8">' +
    '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">' +
    "<title>Trips</title>" +
    '<link rel="stylesheet" href="' + BASE + "/static/app.css?v=" + VERSION + '"></head><body>' +
    '<header class="top"><a class="back" href="' + BASE + '/" aria-label="All trips">‹</a>' +
    "<h1>Trips</h1></header>" +
    '<main class="page"><p class="empty" role="status">' + sentence + "</p></main></body></html>";
  return new Response(html, {
    status: 503,
    headers: {
      "Content-Type": "text/html; charset=utf-8",
      "Cache-Control": "no-store",
      "Content-Security-Policy":
        "default-src 'self'; script-src 'none'; style-src 'self'; img-src 'self'; font-src 'self';" +
        " form-action 'self'; base-uri 'none'; object-src 'none'; frame-ancestors 'none'",
      "X-Content-Type-Options": "nosniff",
    },
  });
}

// The promise's answer within `ms`, else null (and null at once when it fails).
function within(promise, ms) {
  return new Promise((resolve) => {
    const timer = setTimeout(() => resolve(null), ms);
    promise.then(
      (r) => { clearTimeout(timer); resolve(r); },
      () => { clearTimeout(timer); resolve(null); }
    );
  });
}

function redirected(r) {
  return r.redirected || r.type === "opaqueredirect" || (r.status >= 300 && r.status < 400);
}

// What a page's answer from the network does to the store: a 401, a 403, a redirect or a 200 the
// server did not mark (the session is over, or a login stands in front, perhaps answering at the
// page's own address) empties it; a 404 drops that page's copy; a 200 the server marked, not
// redirected, of an address with no query, is kept.
async function settle(r, key, hasQuery) {
  const unmarked = r.status === 200 && r.headers.get(KEEP) !== "1";
  if (r.status === 401 || r.status === 403 || redirected(r) || unmarked) {
    await caches.delete(PAGES);
  } else if (r.status === 404) {
    if (await caches.has(PAGES)) await (await caches.open(PAGES)).delete(key);
  } else if (r.status === 200 && r.headers.get(KEEP) === "1" && !hasQuery) {
    await keep(key, r);
  }
}

async function keep(key, r) {
  const headers = new Headers(r.headers);
  headers.set(SAVED, new Date().toISOString());
  const body = await r.arrayBuffer();
  const store = await caches.open(PAGES);
  await store.put(key, new Response(body, { status: 200, headers }));
  const keys = await store.keys();
  if (keys.length <= MOST) return;
  const dated = await Promise.all(keys.map(async (k) => [k, await savedAt(store, k)]));
  dated.sort((a, b) => a[1] - b[1]);
  for (const [k] of dated.slice(0, dated.length - MOST)) await store.delete(k);
}

async function savedAt(store, key) {
  const r = await store.match(key);
  return (r && Date.parse(r.headers.get(SAVED) || "")) || 0;
}

// The saved copy of `key`, its <html> tag marked with when it was saved; null for none. A store
// whose newest copy is over MAX_AGE_MS old is emptied first.
async function savedCopy(key) {
  if (!(await caches.has(PAGES))) return null;
  const store = await caches.open(PAGES);
  let newest = 0;
  for (const k of await store.keys()) newest = Math.max(newest, await savedAt(store, k));
  if (Date.now() - newest > MAX_AGE_MS) {
    await caches.delete(PAGES);
    return null;
  }
  const r = await store.match(key);
  if (!r) return null;
  const when = r.headers.get(SAVED) || "";
  const html = (await r.text()).replace(/<html\b/, '<html data-saved="' + when + '"');
  const headers = new Headers(r.headers);
  headers.delete("Content-Length");
  return new Response(html, { status: 200, headers });
}

// The list or a trip: the network's answer as soon as it comes, what it does to the store done
// beside it (its body read whole, a write), so a slow body or a store that cannot be written
// changes no answer. A page with a saved copy waits WAIT_MS for the network, then shows the
// copy; one with none waits for the network as long as it takes, and says it has no copy only
// when the network fails.
async function page(event, url) {
  const key = url.origin + url.pathname;
  const network = fetch(event.request);
  event.waitUntil(
    network.then((r) => settle(r.clone(), key, url.search !== "")).catch(() => null)
  );
  const answer = await within(network, WAIT_MS);
  if (answer) return answer;
  const copy = await savedCopy(key).catch(() => null);
  return copy || network.catch(() => said(NO_COPY));
}

// Any other page: the network; with none, the page that says there is no copy. A 401, a 403 or
// a redirect empties the store as above; a store that cannot be emptied changes no answer.
async function other(event) {
  let r;
  try {
    r = await fetch(event.request);
  } catch (e) {
    return said(NO_COPY);
  }
  if (r.status === 401 || r.status === 403 || redirected(r)) {
    await caches.delete(PAGES).catch(() => null);
  }
  return r;
}

// A change from one of this page's own pages: sent as it is, never queued; with no network, the
// page that says it was not saved. A 401 or a 403 (the session is over) empties the pages store;
// a store that cannot be emptied changes no answer.
async function change(asked) {
  let r;
  try {
    r = await fetch(asked);
  } catch (e) {
    return said(NOT_SAVED);
  }
  if (r.status === 401 || r.status === 403) await caches.delete(PAGES).catch(() => null);
  return r;
}

// A static file: from its store, else the network (a 200 is stored); a store that cannot be
// read or written changes no answer.
async function file(request) {
  let store = null;
  try {
    store = await caches.open(STATIC);
    const kept = await store.match(request);
    if (kept) return kept;
  } catch (e) {
    store = null;
  }
  const r = await fetch(request);
  if (store && r.status === 200 && !r.redirected) {
    await store.put(request, r.clone()).catch(() => null);
  }
  return r;
}

self.addEventListener("fetch", (event) => {
  const asked = event.request;
  const url = new URL(asked.url);
  const path = local(url);
  if (path === null) return;
  if (UNTOUCHED.includes(path) || UNTOUCHED_UNDER.some((p) => path.startsWith(p))) return;
  if (asked.method !== "GET") {
    // a change from one of this page's own pages is the worker's (`change`); any other is left
    // to the browser, never sent again from here
    if (!asked.referrer.startsWith(self.location.origin + BASE + "/")) return;
    event.respondWith(change(asked));
    return;
  }
  if (path.startsWith("/static/")) {
    event.respondWith(file(asked));
  } else if (path === "/" || TRIP.test(path)) {
    event.respondWith(page(event, url));
  } else if (asked.mode === "navigate") {
    event.respondWith(other(event));
  }
});
