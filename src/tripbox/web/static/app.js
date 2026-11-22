// SPDX-License-Identifier: AGPL-3.0-only
// Additional terms: see NOTICE
(function () {
  "use strict";
  const $ = (s, el) => (el || document).querySelector(s);
  const $$ = (s, el) => Array.from((el || document).querySelectorAll(s));
  const toast = (msg, ms) => {
    const t = $("#toast");
    if (!t) return;
    t.hidden = false; // shown first, so the status region announces the words put in it
    t.textContent = msg;
    clearTimeout(t._h);
    t._h = setTimeout(() => (t.hidden = true), ms || 1600);
  };
  // a redirect's ?f=<key> becomes one toast, then leaves the URL so a reload stays quiet
  const flashed = $("#toast");
  const stripped = !!(flashed && flashed.dataset.flash);
  if (stripped) {
    toast(flashed.dataset.flash, 4000);
    const q = new URLSearchParams(location.search);
    q.delete("f");
    history.replaceState(null, "", location.pathname + (q.size ? `?${q}` : "") + location.hash);
  }
  // a sheet opened near the bottom of the screen scrolls into view (toggle does not bubble)
  document.addEventListener("toggle", (ev) => {
    const d = ev.target;
    if (d.open && d.matches && d.matches("details.menu, details.item-menu")) {
      const s = d.querySelector(".sheet");
      if (s) s.scrollIntoView({ block: "nearest" });
    }
  }, true);
  // a tap anywhere outside an open ⋯ sheet closes it (⋯ itself still toggles); that tap does
  // nothing else, except on another ⋯, which opens in the same tap. pointerup, not click: iOS
  // sends no click for a tap on plain page background, and a scroll ends in pointercancel, so
  // scrolling never closes a sheet.
  const MENUS = "details.menu, details.item-menu";
  let dismissedAt = 0;
  document.addEventListener("pointerdown", () => (dismissedAt = 0), true);
  document.addEventListener("pointerup", (ev) => {
    const t = ev.target instanceof Element ? ev.target : null;
    const open = $$(MENUS).filter((d) => d.open && !(t && d.contains(t)));
    if (!open.length) return;
    open.forEach((d) => (d.open = false));
    if (!(t && t.closest(`:is(${MENUS}) > summary`))) dismissedAt = ev.timeStamp;
  }, true);
  document.addEventListener("click", (ev) => {
    if (dismissedAt && ev.timeStamp - dismissedAt < 600) {
      ev.preventDefault();
      ev.stopPropagation();
    }
    dismissedAt = 0;
  }, true);
  // Escape closes every open ⋯ sheet and puts the focus back on the ⋯ that opened it (the one
  // holding the focus, else the last open one in the page's order)
  document.addEventListener("keydown", (ev) => {
    if (ev.key !== "Escape") return;
    const open = $$(MENUS).filter((d) => d.open);
    if (!open.length) return;
    const from = open.find((d) => d.contains(document.activeElement)) || open[open.length - 1];
    open.forEach((d) => (d.open = false));
    const summary = from.querySelector(":scope > summary");
    if (summary) summary.focus();
  });
  // ---- saved copies (the service worker, sw.js): it is registered from <base>/sw.js under
  // <base>/ only while the page names it (data-sw); a saved copy it serves (data-saved) says so
  // and changes nothing; a sign-out, and the button in Settings, first empty the pages store.
  const root = document.documentElement;
  const base = root.dataset.base || "";
  const PAGES = "tripbox" + base + ":pages:"; // every release's pages store of this base
  const forget = async () => {
    try {
      if (!window.caches) return; // a page not over https nor on localhost keeps nothing
      const names = await caches.keys();
      await Promise.all(names.filter((n) => n.startsWith(PAGES)).map((n) => caches.delete(n)));
    } catch (e) { /* no store to reach */ }
  };
  const sw = root.dataset.sw;
  const savedAt = root.dataset.saved;
  // The list or a trip in view is asked for again, so the worker keeps it as it now stands: once
  // a worker first controls the page (the load that registered it came before it), and once a
  // redirect's ?f= has left the address (an address with a query is never kept). A GET of the
  // path alone, its answer and its errors left to the worker; never on a saved copy.
  const KEPT_HERE = /^\/(trips\/[0-9]+)?$/;
  const keepHere = () => {
    if (savedAt || !sw || !navigator.serviceWorker || !navigator.serviceWorker.controller) return;
    const path = location.pathname;
    if (!path.startsWith(base + "/") || !KEPT_HERE.test(path.slice(base.length))) return;
    fetch(path, { credentials: "same-origin" }).catch(() => {});
  };
  if (sw && "serviceWorker" in navigator) {
    navigator.serviceWorker.addEventListener("controllerchange", keepHere, { once: true });
    navigator.serviceWorker.register(sw, { scope: base + "/" }).catch(() => {});
    if (stripped) keepHere();
  }
  if (savedAt) {
    const at = Date.parse(savedAt);
    const mins = Number.isNaN(at) ? 0 : Math.max(0, Math.floor((Date.now() - at) / 60000));
    const age = mins < 1 ? "just now"
      : mins < 60 ? `${mins} min ago`
      : mins < 1440 ? `${Math.floor(mins / 60)} h ago`
      : `${Math.floor(mins / 1440)} d ago`;
    const banner = document.createElement("p");
    banner.className = "saved-banner";
    banner.setAttribute("role", "status");
    banner.textContent = `Saved copy · ${age}. You are offline, so nothing can be changed here.`;
    document.body.prepend(banner);
    // every control is disabled but a copy button: copying a confirmation code changes nothing
    $$("input, select, textarea, button").forEach((el) => {
      if (!el.matches("[data-copy]")) el.disabled = true;
    });
    // the ⋯ sheets and Add notes stay closed, and the share link (a page with no copy) goes nowhere
    const LOCKED = MENUS + ", details.inline";
    $$(LOCKED).forEach((d) => {
      d.open = false;
      const s = d.querySelector(":scope > summary");
      if (s) { s.setAttribute("aria-disabled", "true"); s.tabIndex = -1; }
    });
    $$("a[data-share]").forEach((a) => a.setAttribute("aria-disabled", "true"));
    document.addEventListener("click", (ev) => {
      const t = ev.target instanceof Element ? ev.target : null;
      if (t && t.closest(`:is(${LOCKED}) > summary, a[data-share]`)) {
        ev.preventDefault();
        ev.stopPropagation();
      }
    }, true);
  }
  // go() once the pages store is empty, or after two seconds whatever happened
  const signingOut = (go) => {
    let gone = false;
    const once = () => { if (!gone) { gone = true; go(); } };
    forget().then(once, once);
    setTimeout(once, 2000);
  };
  document.addEventListener("click", (ev) => {
    const a = ev.target instanceof Element ? ev.target.closest("a[data-signout]") : null;
    if (!a) return;
    ev.preventDefault();
    signingOut(() => location.assign(a.href));
  });
  const forgetButton = $("[data-forget]");
  if (forgetButton) {
    forgetButton.addEventListener("click", () => {
      forgetButton.disabled = true;
      forget().then(() => toast("Saved copies removed from this device.", 4000));
    });
  }
  async function copyText(text) {
    try {
      await navigator.clipboard.writeText(text);
    } catch (e) {
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.setAttribute("readonly", "");
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      document.execCommand("copy");
      ta.remove();
    }
    toast("Copied");
  }
  document.addEventListener("click", (ev) => {
    const b = ev.target.closest("[data-copy]");
    if (b) {
      ev.preventDefault();
      copyText(b.getAttribute("data-copy"));
      const menu = b.closest("details");
      if (menu) menu.open = false;
      return;
    }
    const p = ev.target.closest("[data-copy-preview]");
    if (p) {
      if (document.body.dataset.pending === "1") { toast("Updating…"); return; }
      copyText($("#preview").textContent);
    }
  });
  document.addEventListener("submit", (ev) => {
    const f = ev.target;
    if (f.dataset.confirm && !confirm(f.dataset.confirm)) { ev.preventDefault(); return; }
    if (f.hasAttribute("data-signout")) { // sent once the saved copies are gone
      ev.preventDefault();
      signingOut(() => HTMLFormElement.prototype.submit.call(f));
    }
    // one request per tap: a second tap over a slow tunnel must not merge or remove twice.
    // Disabled after the browser has serialised the form, so button values still post.
    setTimeout(() => $$('button[type="submit"], button:not([type])', f).forEach((b) => (b.disabled = true)), 0);
  });
  // Safari's back-forward cache restores the DOM as it was, disabled buttons included
  // (never on a saved copy, whose controls stay disabled)
  window.addEventListener("pageshow", () => {
    if (!root.dataset.saved) $$("button[disabled]").forEach((b) => (b.disabled = false));
  });
  // ---- refresh: the pages that opt in (<body data-refresh>: the list, a trip, Recently deleted)
  // reload when you come back after STALE_MS away and, in the home-screen app (no Safari chrome,
  // so no native pull-to-refresh or reload button), on a pull-down from the top. Never while a
  // sheet or an inline form is open or a field has focus: a reload would drop what was typed.
  if (document.body.hasAttribute("data-refresh")) {
    const STALE_MS = 3 * 60 * 1000;
    const PULL_PX = 72; // finger travel that arms the reload
    const busy = () =>
      !!$("details[open]") || !!(document.activeElement && document.activeElement.matches("input, textarea, select"));
    let hiddenAt = 0;
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "hidden") hiddenAt = Date.now();
      else if (hiddenAt && Date.now() - hiddenAt >= STALE_MS && !busy()) location.reload();
    });
    const standalone = navigator.standalone === true || matchMedia("(display-mode: standalone)").matches;
    if (standalone) {
      const pill = document.createElement("div");
      pill.className = "ptr";
      pill.hidden = true;
      pill.setAttribute("aria-hidden", "true");
      document.body.appendChild(pill);
      let y0 = null;
      let armed = false;
      const reset = () => { y0 = null; armed = false; pill.hidden = true; };
      document.addEventListener("touchstart", (ev) => {
        reset();
        if (ev.touches.length === 1 && scrollY <= 0 && !busy()) y0 = ev.touches[0].clientY;
      }, { passive: true });
      document.addEventListener("touchmove", (ev) => {
        if (y0 === null) return;
        const d = ev.touches[0].clientY - y0;
        if (d <= 0 || scrollY > 0) { reset(); return; } // an upward swipe is a scroll, not a pull
        armed = d >= PULL_PX;
        pill.hidden = false;
        pill.style.opacity = Math.min(1, d / PULL_PX);
        pill.classList.toggle("armed", armed);
        pill.textContent = armed ? "Release to refresh" : "Pull to refresh";
      }, { passive: true });
      document.addEventListener("touchend", () => {
        if (!armed) { reset(); return; }
        pill.textContent = "Refreshing…";
        location.reload();
      }, { passive: true });
      document.addEventListener("touchcancel", reset, { passive: true });
    }
  }
  // ---- share page
  const form = $("#share-form");
  if (!form) return;
  const preview = $("#preview");
  const shareBtn = $("#share");
  if (navigator.share) {
    shareBtn.hidden = false;
    shareBtn.addEventListener("click", () => navigator.share({ text: preview.textContent }).catch(() => {}));
  }
  let seq = 0;
  async function refresh() {
    const items = $$('input[name="items"]:checked', form).map((i) => i.value);
    const flag = (n) => { const i = $(`input[name="${n}"]`, form); return i && !i.checked ? "0" : "1"; };
    const url = `${form.dataset.textUrl}?items=${items.join(",")}&conf=${flag("conf")}&names=${flag("names")}&notes=${flag("notes")}`;
    const my = ++seq;
    document.body.dataset.pending = "1";
    try {
      const r = await fetch(url, { credentials: "same-origin" });
      // an error page (401 after the Access session lapsed, a 404 for a merged trip) must
      // never become the text that gets copied into a message
      if (r.ok && my === seq) preview.textContent = await r.text();
      else if (!r.ok) toast(r.status === 401 ? "Session expired — reload" : "Could not update");
    } catch (e) { toast("Offline — showing the last preview"); }
    if (my === seq) delete document.body.dataset.pending;
  }
  form.addEventListener("change", refresh);
  $$(".chip-btn").forEach((c) =>
    c.addEventListener("click", () => {
      const kind = c.dataset.kind;
      $$(".chip-btn").forEach((x) => x.classList.toggle("on", x === c));
      $$('input[name="items"]', form).forEach((i) => {
        const cancelled = i.closest(".pick").classList.contains("cancelled");
        i.checked = kind === "all" ? !cancelled : i.dataset.kind === kind && !cancelled;
      });
      refresh();
    })
  );
})();
