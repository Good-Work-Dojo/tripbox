# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""The checks of the page that shoot.py runs once its pictures are taken. Each one is an entry
{name, ok, detail} of the run's report.json; a failure's detail names the page and the element or
request at fault, never a bare count.

    csp               no page broke its own content policy: each planned page is loaded with
                      CSP_LISTENER, which keeps every `securitypolicyviolation` event (CSP_READ;
                      a load with no listener leaves its page not looked at), and once a run the
                      check proves itself: an inline style planted on the first page must be
                      reported (CSP_PROBE)
    other-origins     no page asked anything of another origin (blocked and recorded per page)
    outside-the-base  no page asked its own origin for a path outside its base path
    labels            no visible input, select, textarea, button, summary or a[href] without an
                      accessible name, on each planned page with every <details> opened (UNNAMED)
    escape            each ⋯ sheet of one trip page opens, closes on Escape and leaves the focus on
                      its ⋯ (`escape_problems`)
    protected         with no cookie each planned page lands on the login page, and again after
                      sign-out with the ended session's cookie sent back (`landing_problems`);
                      no page loaded is a failure
    shared            the shared page of a link, loaded with no cookie, holds no script element
                      (SCRIPTS) and asked for nothing but itself, the stylesheet, the font and
                      the icon;
                      a wrong link is a 404 with an empty body; opened signed in, it is in no
                      store of the service worker's (`shared_problems`)
    offline           saved copies, in a browser context of its own whose network is cut by a
                      route that aborts every request: the cut must reach a request the service
                      worker itself makes, else the check says it could not test; then (1) the
                      worker's scope is the base, (2) a trip opened online reloads offline as a
                      saved copy (its title, the banner, every control but a copy button, every
                      ⋯, Add notes and the share link disabled), (3) a trip never
                      opened gives the no-copy page and a form posted from the copy the refusal,
                      (4) no store holds a page under /login, /s/, /settings or /add, (5) after
                      sign-out the trip has no copy and the pages store is empty, (6) Chromium
                      finds the manifest with no error, (7) the list sign-in lands on, loaded
                      before the worker controlled it, is kept once it does and reloads offline
                      as its saved copy (`offline_problems`)

Nothing here imports Playwright: what drives a browser takes the page shoot.py hands it and asks
it only `evaluate` and `keyboard.press`; the rest judges plain data.
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

NAMES = (
    "csp",
    "other-origins",
    "outside-the-base",
    "labels",
    "escape",
    "protected",
    "shared",
    "offline",
)
SHOWN = 8  # the problems a failure's detail names before it says how many more there are
MENUS = "details.menu, details.item-menu"  # a ⋯ and its sheet
LOCKED = MENUS + ", details.inline"  # what a saved copy keeps closed: the ⋯ sheets and Add notes

# Run in every frame before the page's own code (an init script, outside the page's policy):
# keeps every violation of the page's content policy as the page saw it.
CSP_LISTENER = """(() => {
  if (window.__tripboxViolations) return;
  const seen = [];
  Object.defineProperty(window, "__tripboxViolations", { value: seen });
  const describe = (node) => {
    if (!node || node.nodeType !== 1) return node && node.nodeType === 9 ? "the document" : "";
    let s = node.tagName.toLowerCase();
    if (node.id) s += "#" + node.id;
    const classes = node.classList ? Array.from(node.classList).slice(0, 2) : [];
    if (classes.length) s += "." + classes.join(".");
    return s;
  };
  document.addEventListener("securitypolicyviolation", (e) => {
    seen.push({
      directive: e.effectiveDirective || e.violatedDirective || "",
      blocked: e.blockedURI || "",
      element: describe(e.target),
      sample: (e.sample || "").slice(0, 60),
    });
  }, true);
})();"""
# What a load kept, or null when the listener is not there (nothing was looked at)
CSP_READ = "() => (window.__tripboxViolations ? window.__tripboxViolations.slice() : null)"
# Once a run, on the first page: an inline style set from script, which the policy refuses, must
# be reported within a short wait; then the record is cleared. "reported", "silent" or "no
# listener".
PROBE_ID = "tripbox-csp-probe"
CSP_PROBE = """async () => {
  const seen = window.__tripboxViolations;
  if (!seen) return "no listener";
  const planted = document.createElement("span");
  planted.id = "{probe_id}";
  planted.hidden = true;
  document.body.appendChild(planted);
  planted.setAttribute("style", "color: red");
  const ours = (v) => v.directive.startsWith("style-src") && v.element === "span#{probe_id}";
  let reported = false;
  for (let waited = 0; !reported && waited < 2000; waited += 50) {
    await new Promise((done) => setTimeout(done, 50));
    reported = seen.some(ours);
  }
  planted.remove();
  seen.length = 0;
  return reported ? "reported" : "silent";
}""".replace("{probe_id}", PROBE_ID)
PLANTED = "the page's policy did not report a planted inline style"

# Every visible control without an accessible name, once every <details> of the page is open,
# each described by its tag, id, name and classes and the nearest element with an id around it.
# The name is worked out as a browser does for these elements: aria-labelledby, aria-label, its
# labels (a form control) or its own text (a button, summary or link), title, then placeholder.
UNNAMED = """() => {
  for (const d of document.querySelectorAll("details")) d.open = true;
  const text = (el) =>
    (el ? el.innerText || el.textContent || "" : "").replace(/\\s+/g, " ").trim();
  const nameOf = (el) => {
    const by = (el.getAttribute("aria-labelledby") || "").split(/\\s+/).filter(Boolean)
      .map((id) => text(document.getElementById(id))).join(" ").trim();
    if (by) return by;
    const label = (el.getAttribute("aria-label") || "").trim();
    if (label) return label;
    const tag = el.tagName.toLowerCase();
    if (tag === "input" || tag === "select" || tag === "textarea") {
      const labels = el.labels ? Array.from(el.labels).map(text).join(" ").trim() : "";
      if (labels) return labels;
      if (tag === "input" && ["submit", "button", "reset"].includes(el.type)) {
        if ((el.value || "").trim()) return el.value.trim();
      }
    } else {
      const alt = Array.from(el.querySelectorAll("img[alt]")).map((i) => i.alt).join(" ");
      if (text(el) || alt.trim()) return text(el) || alt.trim();
    }
    const title = (el.getAttribute("title") || "").trim();
    if (title) return title;
    const typed = tag === "input" || tag === "textarea";
    return typed ? (el.getAttribute("placeholder") || "").trim() : "";
  };
  const visible = (el) => el.checkVisibility
    ? el.checkVisibility({ visibilityProperty: true }) : el.getClientRects().length > 0;
  const out = [];
  for (const el of document.querySelectorAll("input, select, textarea, button, summary, a[href]")) {
    if (el.type === "hidden" || !visible(el) || nameOf(el)) continue;
    let s = el.tagName.toLowerCase();
    if (el.id) s += "#" + el.id;
    if (el.getAttribute("name")) s += '[name="' + el.getAttribute("name") + '"]';
    if (el.classList.length) s += "." + Array.from(el.classList).slice(0, 2).join(".");
    const around = el.parentElement && el.parentElement.closest("[id]");
    out.push(around ? s + " in #" + around.id : s);
  }
  return out;
}"""

# The photo controls of a trip page, in the page whether or not its sheet is open: how many file
# inputs named photo it holds and the text of each button of its photo forms and of Remove photo.
PHOTO_CONTROLS = """() => ({
  file: document.querySelectorAll('input[type="file"][name="photo"]').length,
  buttons: Array.from(document.querySelectorAll(
    'form.photo-form button, form[action$="/cover/remove"] button'))
    .map((b) => b.textContent.trim()),
})"""
UPLOAD_BUTTON = "Upload photo"

# A page that holds a photo on its way reloads itself every few seconds (static/app.js counts the
# reloads in sessionStorage under a key with this word and stops at six). A shot must not be of a
# page in the middle of a reload, so a context of a shot answers that count as already spent:
# the page then stays as it loaded. An init script, so it runs before the page's own.
NO_COVER_RELOAD = """(() => {
  const get = Storage.prototype.getItem;
  Storage.prototype.getItem = function (k) {
    return String(k).includes(":cover-reloads:") ? "99" : get.call(this, k);
  };
})()"""

# The cover images of a page (the trip's hero): each one's address and its natural width once the
# browser has decoded it (0: it did not load).
COVER_IMGS = """async () => {
  const imgs = Array.from(document.querySelectorAll(".hero .cover.photo img"));
  await Promise.all(imgs.map((i) => i.decode().catch(() => {})));
  return imgs.map((i) => ({ src: i.getAttribute("src"), width: i.naturalWidth }));
}"""

# How many script elements the page holds (asked of every load; a shared page must hold none)
SCRIPTS = "() => document.scripts.length"

# The ⋯ sheets of the page: how many there are; where the one at index i is; focus its ⋯; and
# whether it is open and where the focus is.
MENU_COUNT = f"() => document.querySelectorAll({MENUS!r}).length"
MENU_WHERE = f"""(i) => {{
  const d = document.querySelectorAll({MENUS!r})[i];
  const item = d.closest('[id^="item-"]');
  if (item) return "the ⋯ of #" + item.id;
  return d.classList.contains("menu") ? "the trip's ⋯" : "⋯ number " + (i + 1);
}}"""
MENU_FOCUS = f"""(i) => {{
  const s = document.querySelectorAll({MENUS!r})[i].querySelector(":scope > summary");
  s.focus();
  return document.activeElement === s;
}}"""
MENU_STATE = f"""(i) => {{
  const d = document.querySelectorAll({MENUS!r})[i];
  const a = document.activeElement;
  let at = a ? a.tagName.toLowerCase() : "nothing";
  if (a && a.id) at += "#" + a.id;
  const item = a && a.closest && a.closest('[id^="item-"]');
  if (item && item !== a) at += " in #" + item.id;
  return {{ open: d.open, focused: a === d.querySelector(":scope > summary"), active: at }};
}}"""


def result(name: str, problems: list[str], checked: str) -> dict:
    """The entry of the check `name`: ok when there is no problem, its detail what was checked
    (ok) or the first SHOWN problems and how many more (not ok)."""
    if not problems:
        return {"name": name, "ok": True, "detail": checked}
    detail = "; ".join(problems[:SHOWN])
    if len(problems) > SHOWN:
        detail += f"; and {len(problems) - SHOWN} more"
    return {"name": name, "ok": False, "detail": detail}


def _distinct(values: list) -> list:
    return list(dict.fromkeys(values))


def read_violations(page) -> list[dict] | None:
    """What the listener of `page` (a Playwright page) kept; None when it has none."""
    return page.evaluate(CSP_READ)


def probe(page) -> str:
    """Plants an inline style on `page` and says whether its refusal was reported (CSP_PROBE)."""
    return page.evaluate(CSP_PROBE)


def csp(pages: list[dict], probed: str | None) -> dict:
    """Each page's violations of its content policy (`csp`: what CSP_READ gave on each load of
    it; a page with no load read, or a load without the listener, was not looked at), once the
    probe of the run (`probed`, CSP_PROBE's answer; None: it did not run) was reported."""
    problems = [] if probed == "reported" else [PLANTED]
    for p in pages:
        loads = p.get("csp") or []
        if not loads or any(load is None for load in loads):
            problems.append(f"{p['name']}: not looked at")
        seen = [v for load in loads if load for v in load]
        problems += [
            f"{p['name']}: {v.get('directive') or 'a directive'} refused"
            f" {v.get('blocked') or v.get('sample') or 'inline code'}"
            + (f" at {v['element']}" if v.get("element") else "")
            for v in _distinct_violations(seen)
        ]
    return result("csp", problems, f"no violation on {len(pages)} page(s), the probe reported")


def _distinct_violations(violations: list[dict]) -> list[dict]:
    seen: dict[tuple, dict] = {}
    for v in violations:
        seen.setdefault((v.get("directive"), v.get("blocked"), v.get("element")), v)
    return list(seen.values())


def other_origins(pages: list[dict]) -> dict:
    problems = [
        f"{p['name']}: asked {host}" for p in pages for host in _distinct(p["other_origins"])
    ]
    checked = f"no request to another origin from {len(pages)} page(s)"
    return result("other-origins", problems, checked)


def outside_the_base(pages: list[dict]) -> dict:
    problems = [
        f"{p['name']}: asked {path}" for p in pages for path in _distinct(p["outside_base"])
    ]
    checked = f"no request outside the base from {len(pages)} page(s)"
    return result("outside-the-base", problems, checked)


def labels(pages: list[dict]) -> dict:
    """Each page's controls without a name (`unnamed`: what UNNAMED gave); a page that was not
    looked at (it did not load) is a problem too."""
    problems: list[str] = []
    for p in pages:
        if p.get("unnamed") is None:
            problems.append(f"{p['name']}: not looked at")
            continue
        problems += [f"{p['name']}: {element} has no name" for element in _distinct(p["unnamed"])]
        photo = p.get("photo")  # a trip page: its file input and upload button were looked at too
        if photo is not None and (photo["file"] < 1 or UPLOAD_BUTTON not in photo["buttons"]):
            problems.append(f"{p['name']}: the photo file input and {UPLOAD_BUTTON} were not found")
    return result(
        "labels", problems, f"every control named on {len(pages)} page(s), the photo controls too"
    )


def escape_problems(page) -> list[str]:
    """On `page` (a Playwright page showing a trip, signed in): each ⋯, focused and opened with
    Enter, opens its sheet; Escape closes it and leaves the focus on that ⋯."""
    count = page.evaluate(MENU_COUNT)
    if not count:
        return ["no ⋯ sheet on the page"]
    problems: list[str] = []
    for i in range(count):
        where = page.evaluate(MENU_WHERE, i)
        if not page.evaluate(MENU_FOCUS, i):
            problems.append(f"{where}: cannot take the focus")
            continue
        page.keyboard.press("Enter")
        if not page.evaluate(MENU_STATE, i)["open"]:
            problems.append(f"{where}: Enter did not open its sheet")
            continue
        page.keyboard.press("Escape")
        state = page.evaluate(MENU_STATE, i)
        if state["open"]:
            problems.append(f"{where}: Escape left its sheet open")
        elif not state["focused"]:
            problems.append(f"{where}: Escape left the focus on {state['active']}")
    return problems


def escape(trip_name: str | None, problems: list[str]) -> dict:
    """The escape check of the trip page `trip_name` (None when no trip page was planned)."""
    if trip_name is None:
        return result("escape", ["no trip page was planned"], "")
    named = [f"{trip_name}: {problem}" for problem in problems]
    return result("escape", named, f"every ⋯ sheet of {trip_name} closes on Escape")


def landing_problems(landed: dict[str, str], login_path: str, when: str) -> list[str]:
    """`landed`: the path each planned page (by name) ended on, loaded `when` (with no cookie,
    after sign-out); each one must be the login page."""
    return [
        f"{name} {when} landed on {path or 'nothing'}"
        for name, path in landed.items()
        if path != login_path
    ]


def protected(problems: list[str], pages: int) -> dict:
    """The protected check of `pages` pages loaded signed out and their problems; with no page
    loaded it checked nothing, which is never ok."""
    if not pages:
        problems = ["no page was checked", *problems]
    return result(
        "protected",
        problems,
        f"{pages} page(s) land on the login page with no cookie and after sign-out",
    )


def shared_problems(
    page: dict, base_path: str, wrong: tuple[int, bytes], stored: dict | None = None
) -> list[str]:
    """The shared check's problems: the shared page `page` (its entry in report.json: `asked`,
    every path it asked of its own origin; `scripts`, how many script elements each load held,
    None where it was not read) held a script or asked for anything but itself, the stylesheet
    and the icon under base_path and the fonts beside them; `wrong`, the status and body a wrong
    link got, is
    not a 404 with an empty body; `stored`, the service worker's stores (STORED's answer) once
    the page was opened signed in, holds it (None: they were not read)."""
    name = page["name"]
    problems: list[str] = []
    loads = page.get("scripts") or []
    if not loads or any(n is None for n in loads):
        problems.append(f"{name}: not looked at")
    held = [n for n in loads if n]
    if held:
        problems.append(f"{name}: holds {max(held)} script element(s)")
    fonts = base_path + "/static/fonts/"
    for path in _distinct(page.get("asked") or []):
        bare = path.split("?", 1)[0]
        own = path == page["path"] or bare in (
            base_path + "/static/app.css",
            base_path + "/static/favicon.svg",
        )
        font = bare.startswith(fonts) and "/" not in bare[len(fonts) :]
        if not (own or font):
            problems.append(f"{name}: asked {path}")
    if stored is None:
        problems.append(f"{name}: the stores were not read once it was opened signed in")
    elif page["path"].split("?", 1)[0] in kept_paths(stored):
        problems.append(f"{name}: a store holds it")
    status, body = wrong
    if status != 404:
        problems.append(f"a wrong link was answered {status}")
    if body:
        problems.append(f"a wrong link's answer holds {len(body)} byte(s)")
    return problems


def shared(page_name: str | None, problems: list[str]) -> dict:
    """The shared check of the shared page `page_name` (None when none was planned)."""
    if page_name is None:
        return result("shared", ["no shared page was planned"], "")
    checked = f"{page_name} ran no script and asked for nothing else; a wrong link is a bare 404"
    return result("shared", problems, checked)


# ----------------------------------------------------------------------------- offline
# What the page's service worker answers with no network (templates/sw.js), word for word
NO_COPY = (
    "You are offline and this page has no saved copy. Pages you open while connected are kept for"
    " next time."
)
NOT_SAVED = (
    "You are offline, so this was not saved. Go back and try again when you are connected."
)
# The banner of a saved copy (static/app.js)
BANNER = re.compile(
    r"Saved copy · (just now|[0-9]+ min ago|[0-9]+ h ago|[0-9]+ d ago)\. You are offline, so"
    r" nothing can be changed here\."
)
# The pages no store may hold, written from the base: a prefix ending in "/" takes what lies
# under it; any other, itself and what lies under it
NEVER_KEPT = ("/login", "/s/", "/settings", "/add")
CUT = "internetdisconnected"  # how the cut network aborts a request

# Once the page has loaded: the service worker's registration, once it is ready (10 s at most),
# and whether one controls the page.
WORKER_READY = """async () => {
  if (!("serviceWorker" in navigator)) return { scope: null, controlled: false };
  const late = new Promise((done) => setTimeout(() => done(null), 10000));
  const reg = await Promise.race([navigator.serviceWorker.ready, late]);
  return { scope: reg ? reg.scope : null, controlled: !!navigator.serviceWorker.controller };
}"""
# Whether a pages store holds the address `url`, asked every 100 ms for 10 s at most (the page
# asks for itself again once the worker controls it, and the worker keeps the answer).
KEPT_SOON = """async (url) => {
  if (!window.caches) return false;
  for (let waited = 0; waited <= 10000; waited += 100) {
    for (const name of await caches.keys()) {
      if (name.includes(":pages:") && (await (await caches.open(name)).match(url))) return true;
    }
    await new Promise((done) => setTimeout(done, 100));
  }
  return false;
}"""
# Every store of the origin and the address of each response it holds; null with no Cache API.
STORED = """async () => {
  if (!window.caches) return null;
  const out = {};
  for (const name of await caches.keys()) {
    out[name] = (await (await caches.open(name)).keys()).map((r) => r.url);
  }
  return out;
}"""
# What a page shows: its <html> tag's data-saved, the banner's words, its heading, its text,
# every form control that is not disabled but a copy button (copying changes nothing), every ⋯
# and Add notes that is not disabled, and the share link when it is not marked disabled.
SHOWN_NOW = f"""() => {{
  const describe = (el) => {{
    let s = el.tagName.toLowerCase();
    if (el.id) s += "#" + el.id;
    if (el.getAttribute("name")) s += '[name="' + el.getAttribute("name") + '"]';
    return s;
  }};
  const banner = document.querySelector('.saved-banner[role="status"]');
  const h1 = document.querySelector("h1");
  return {{
    saved: document.documentElement.dataset.saved || null,
    banner: banner ? banner.textContent : null,
    title: h1 ? h1.textContent.trim() : null,
    text: (document.body ? document.body.innerText : "").replace(/\\s+/g, " ").trim(),
    enabled: Array.from(document.querySelectorAll("input, select, textarea, button"))
      .filter((el) => el.type !== "hidden" && !el.disabled && !el.matches("[data-copy]"))
      .map(describe),
    menus: Array.from(document.querySelectorAll({LOCKED!r}))
      .filter((d) => d.querySelector(":scope > summary").getAttribute("aria-disabled") !== "true")
      .map((d) => {{
        if (d.classList.contains("inline")) return "Add notes";
        const item = d.closest('[id^="item-"]');
        return item ? "the ⋯ of #" + item.id : "the trip's ⋯";
      }}),
    links: Array.from(document.querySelectorAll("a[data-share]"))
      .filter((a) => a.getAttribute("aria-disabled") !== "true").map(() => "the share link"),
  }};
}}"""
# Post a form of the page by script, whatever its controls say (the first post form it holds,
# else one made to post to the page's own notes); returns where it posts.
POST_BY_SCRIPT = """() => {
  let f = document.querySelector('form[method="post"]');
  if (!f) {
    f = document.createElement("form");
    f.method = "post";
    f.action = location.pathname + "/notes";
    document.body.appendChild(f);
  }
  HTMLFormElement.prototype.submit.call(f);
  return f.action;
}"""


def _under(path: str, base_path: str, prefix: str) -> bool:
    """Whether `path` lies at `prefix` (written from the base) or under it."""
    if not path.startswith(base_path + "/"):
        return False
    rest = path[len(base_path) :]
    if prefix.endswith("/"):
        return rest.startswith(prefix)
    return rest == prefix or rest.startswith(prefix + "/")


def kept_paths(stored: dict | None) -> list[str]:
    """The paths of every response any store holds (STORED's answer)."""
    return [urlparse(url).path for urls in (stored or {}).values() for url in urls]


def pages_kept(stored: dict | None) -> list[str]:
    """The addresses the pages stores hold (a name holding ":pages:")."""
    return [url for name, urls in (stored or {}).items() if ":pages:" in name for url in urls]


def offline_problems(seen: dict) -> list[str]:
    """The offline check's problems, from what the run saw (`seen`, as shoot.py records it):
    `scope` and `want_scope` (1); `copy`, the trip reloaded offline as SHOWN_NOW saw it, with its
    `status`, and `title`, its heading online (2); `no_copy` and `refused`, a never-opened trip
    and a post from the copy, each {status, text} (3); `stored`, STORED's answer once the pages
    of NEVER_KEPT were visited online, and `kept`, the list's and the trip's paths, which it must
    hold (4); `after`, the trip loaded offline after sign-out ({status, text, saved}, or `error`
    when the load failed with no worker left to answer it, which only a sign-out at the root may
    leave: it clears the site's storage, worker and all) and `after_stored` (5); `manifest`,
    Page.getAppManifest's answer (6); `list_kept`, whether a store held the list sign-in landed
    on once the worker controlled it (KEPT_SOON), `list_title`, its heading online, and
    `list_copy`, that list reloaded offline as SHOWN_NOW saw it, with its `status` (7);
    `cover_online` and `cover_copy`, COVER_IMGS's answer for the trip online and reloaded
    offline, and `want_cover`, whether the trip must hold a cover (8).
    `base_path` is the base. A step whose record is missing was not reached."""
    base_path = seen.get("base_path", "")
    problems: list[str] = [seen["error"]] if seen.get("error") else []
    # (1) the scope
    if seen.get("scope") is None:
        problems.append("(1) no service worker is registered")
    elif seen["scope"] != seen.get("want_scope"):
        problems.append(f"(1) the worker's scope is {seen['scope']}, not {seen.get('want_scope')}")
    # (2) a saved copy
    copy = seen.get("copy")
    if copy is None:
        problems.append("(2) the trip was not reloaded offline")
    else:
        if copy.get("status") != 200 or not copy.get("saved"):
            status = copy.get("status")
            problems.append(f"(2) the trip reloaded offline was answered {status}, not its copy")
        if copy.get("title") != seen.get("title"):
            shown, want = copy.get("title"), seen.get("title")
            problems.append(f"(2) the saved copy's title is {shown!r}, not {want!r}")
        if not BANNER.fullmatch(copy.get("banner") or ""):
            problems.append(f"(2) the saved copy's banner says {copy.get('banner')!r}")
        problems += [f"(2) {el} is not disabled" for el in _distinct(copy.get("enabled") or [])]
        problems += [f"(2) {m} is not disabled" for m in _distinct(copy.get("menus") or [])]
        problems += [f"(2) {a} is not disabled" for a in _distinct(copy.get("links") or [])]
    # (3) no copy, and a change refused
    for step, key, words, what in (
        ("(3)", "no_copy", NO_COPY, "a trip never opened"),
        ("(3)", "refused", NOT_SAVED, "a form posted from the saved copy"),
    ):
        got = seen.get(key)
        if got is None:
            problems.append(f"{step} {what} was not tried")
        elif got.get("status") != 503 or words not in (got.get("text") or ""):
            status = got.get("status")
            problems.append(f"{step} {what} was answered {status}, not {words[:40]!r}…")
    # (4) what is never kept
    stored = seen.get("stored")
    if stored is None:
        problems.append("(4) the stores were not read")
    else:
        paths = kept_paths(stored)
        for prefix in NEVER_KEPT:
            held = [p for p in _distinct(paths) if _under(p, base_path, prefix)]
            problems += [f"(4) a store holds {p}" for p in held]
        problems += [f"(4) no store holds {p}" for p in seen.get("kept") or [] if p not in paths]
        page = re.compile(
            re.escape(base_path) + r"/((trips/[0-9]+)?|covers/[0-9a-f]{64}(-s)?\.jpg)"
        )
        for url in pages_kept(stored):  # the list, the trips and their cover photos alone
            path = urlparse(url).path
            if not page.fullmatch(path):
                problems.append(f"(4) the pages store holds {path}")
    # (5) after sign-out
    after = seen.get("after")
    if after is None:
        problems.append("(5) the trip was not loaded offline after sign-out")
    elif after.get("saved"):
        problems.append("(5) after sign-out the trip opened as a saved copy")
    elif after.get("error") is not None:
        if base_path:  # under a sub-path the worker stays, and must answer
            problems.append(f"(5) after sign-out the trip did not load ({after['error']})")
    elif after.get("status") != 503 or NO_COPY not in (after.get("text") or ""):
        problems.append(f"(5) after sign-out the trip was answered {after.get('status')}")
    left = pages_kept(seen.get("after_stored"))
    problems += [f"(5) after sign-out the pages store holds {url}" for url in left]
    # (6) the manifest
    manifest = seen.get("manifest")
    if manifest is None:
        problems.append("(6) the manifest was not asked for")
    else:
        if not manifest.get("url"):
            problems.append("(6) Chromium found no manifest")
        problems += [
            "(6) " + ("critical: " if e.get("critical") else "") + str(e.get("message", ""))
            for e in manifest.get("errors") or []
        ]
    # (7) the list the worker was installed on
    if seen.get("list_kept") is False:
        problems.append("(7) no store held the list within 10 s of the worker taking control")
    listed = seen.get("list_copy")
    if listed is None:
        problems.append("(7) the list sign-in landed on was not reloaded offline")
    else:
        if listed.get("status") != 200 or not listed.get("saved"):
            status = listed.get("status")
            problems.append(f"(7) the list reloaded offline was answered {status}, not its copy")
        if listed.get("title") != seen.get("list_title"):
            shown, want = listed.get("title"), seen.get("list_title")
            problems.append(f"(7) the list's saved copy's title is {shown!r}, not {want!r}")
        if not BANNER.fullmatch(listed.get("banner") or ""):
            problems.append(f"(7) the list's saved copy's banner says {listed.get('banner')!r}")
    # (8) a saved trip's cover is saved with it
    online, copy_imgs = seen.get("cover_online"), seen.get("cover_copy")
    if seen.get("want_cover") and not online:
        problems.append("(8) the trip opened online holds no cover photo")
    if online:
        if copy_imgs is None:
            problems.append("(8) the cover photo was not looked at in the saved copy")
        else:
            if len(copy_imgs) != len(online):
                problems.append(
                    f"(8) the saved copy shows {len(copy_imgs)} cover photo(s), not {len(online)}"
                )
            problems += [
                f"(8) the saved copy's cover {i['src']} did not load" for i in copy_imgs
                if not i.get("width")
            ]
    return problems


def offline(seen: dict | None, problems: list[str]) -> dict:
    """The offline check: not ok with what could not be tested (`seen["untested"]`; no `seen`:
    it did not run), else its problems."""
    if seen is None:
        return result("offline", ["could not test: the check did not run"], "")
    if seen.get("untested"):
        return {"name": "offline", "ok": False, "detail": "could not test: " + seen["untested"]}
    checked = (
        "the worker is scoped to the base, the list it was installed on and a trip opened online"
        " open offline as their saved copies with every control but a copy button disabled,"
        " nothing else is kept, a saved trip shows its cover photo,"
        " sign-out empties the store, and the manifest has no error"
    )
    return result("offline", problems, checked)


def failed(checks: list[dict]) -> str | None:
    """The name of the first check that is not ok, or None."""
    return next((c["name"] for c in checks if not c["ok"]), None)


def tally(checks: list[dict]) -> str:
    return f"checks: {sum(1 for c in checks if c['ok'])} of {len(checks)} ok"
