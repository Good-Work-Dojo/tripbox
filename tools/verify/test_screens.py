# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""The screenshot harness: its page discovery and naming helpers, and the shape of its stack."""
import importlib.util
import re
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import yaml

SCREENS = Path(__file__).resolve().parent / "screens"
README = Path(__file__).resolve().parent / "README.md"
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


def load_shoot():
    spec = importlib.util.spec_from_file_location("shoot", SCREENS / "shoot.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_links_are_found_in_page_order_under_the_base_path():
    shoot = load_shoot()
    html = (
        '<a href="/tripbox/trips/3">a</a> <a href="/tripbox/trips/1">b</a>'
        ' <a href="/tripbox/trips/3">c</a> <a href="/other/trips/9">d</a>'
        ' <a href="/tripbox/trips/3/share">s</a> <a href="/tripbox/trips/4#x">e</a>'
    )
    assert shoot.trip_links(html, "/tripbox") == ["/tripbox/trips/3", "/tripbox/trips/1"]
    assert shoot.trip_links('<a href="/trips/3">a</a> <a href="/trips/1">b</a>', "") == [
        "/trips/3",
        "/trips/1",
    ]
    edits = (
        '<a href="/other/items/5/edit" data-edit="flight">w</a>'
        ' <a class="btn link" href="/tripbox/items/7/edit" data-edit="lodging">x</a>'
        ' <a data-edit="flight" href="/tripbox/items/2/edit">y</a>'
        ' <a href="/tripbox/items/8/edit" data-edit="lodging">z</a>'
        ' <a href="/tripbox/items/3/edit">no kind</a>'
        ' <a href="/tripbox/items/4/edit" data-edit="train">t</a>'
    )
    assert shoot.edit_links(edits, "/tripbox") == [
        ("lodging", "/tripbox/items/7/edit"),
        ("flight", "/tripbox/items/2/edit"),
        ("train", "/tripbox/items/4/edit"),
    ]
    assert shoot.edit_links("<p>none</p>", "/tripbox") == []


def test_one_edit_page_is_planned_for_each_kind_found(monkeypatch):
    shoot = load_shoot()
    pages = {
        "/tripbox/": '<a href="/tripbox/trips/1">a</a> <a href="/tripbox/trips/2">b</a>',
        "/tripbox/trips/1": (
            '<a href="/tripbox/items/5/edit" data-edit="car">c</a>'
            ' <a href="/tripbox/items/6/edit" data-edit="flight">f</a>'
        ),
        "/tripbox/trips/2": (
            '<a href="/tripbox/items/7/edit" data-edit="flight">f</a>'
            ' <a href="/tripbox/items/8/edit" data-edit="lodging">h</a>'
        ),
    }
    monkeypatch.setattr(
        shoot, "fetch", lambda url, cookie="": (200, pages.get(url.removeprefix("http://web"), ""))
    )
    planned = [(p["name"], p["path"]) for p in shoot.plan_pages("http://web", "/tripbox", 8)]
    edits = [(name, path) for name, path in planned if name.startswith("edit")]
    assert edits == [
        ("edit-flight", "/tripbox/items/6/edit"),
        ("edit-lodging", "/tripbox/items/8/edit"),
        ("edit-car", "/tripbox/items/5/edit"),
    ]
    assert [name for name, _ in planned][-4:] == [
        "deleted",
        "settings",
        "not-found",
        "trip-24h-day-first",
    ]


def test_the_add_pages_and_an_upload_are_planned_before_the_deleted_list(monkeypatch):
    shoot = load_shoot()
    monkeypatch.setattr(shoot, "fetch", lambda url, cookie="": (200, ""))
    planned = shoot.plan_pages("http://web", "/tripbox", 8)
    assert [p["name"] for p in planned][-8:] == [
        "add",
        "add-paste",
        "add-upload",
        "add-flight",
        "upload-status",
        "deleted",
        "settings",
        "not-found",
    ]
    by_name = {p["name"]: p for p in planned}
    assert [by_name[n]["path"] for n in ("add", "add-paste", "add-upload", "add-flight")] == [
        "/tripbox/add",
        "/tripbox/add/paste",
        "/tripbox/add/upload",
        "/tripbox/add/flight",
    ]
    assert {by_name[n]["expect"] for n in ("add", "add-flight")} == {200}
    # the status page is the one the paste form lands on, filled with one fixed line
    status = by_name["upload-status"]
    assert (status["path"], status["expect"]) == ("/tripbox/add/paste", 200)
    assert status["paste"] == shoot.PASTE_LINE
    assert [p["name"] for p in planned if "paste" in p] == ["upload-status"]
    assert "\n" not in shoot.PASTE_LINE and not re.search(r"\d", shoot.PASTE_LINE)
    assert shoot.STATUS_PAGE.search("http://web:8110/tripbox/uploads/Ab-_9xZ")
    for url in ("http://web:8110/tripbox/add/paste", "http://web:8110/tripbox/uploads/"):
        assert not shoot.STATUS_PAGE.search(url), url


def test_settings_and_a_trip_in_the_24_hour_day_first_style_are_planned(monkeypatch):
    """Settings is planned at its fixed path; the first trip again, last of the ledger's pages,
    with the display choices it saves first (24-hour, day first), so every page before it shows
    the default; with no trip, no such page."""
    shoot = load_shoot()
    for base in ("", "/a/b"):
        pages = {f"{base}/": f'<a href="{base}/trips/3">a</a> <a href="{base}/trips/4">b</a>'}

        def fetch(url: str, cookie: str = "", pages=pages) -> tuple[int, str]:
            return 200, pages.get(url.removeprefix("http://web"), "")

        monkeypatch.setattr(shoot, "fetch", fetch)
        planned = shoot.plan_pages("http://web", base, 8, "tripbox_session=s1")
        by_name = {p["name"]: p for p in planned}
        assert (by_name["settings"]["path"], by_name["settings"]["expect"]) == (
            f"{base}/settings",
            200,
        )
        styled = by_name["trip-24h-day-first"]
        assert (styled["path"], styled["expect"]) == (f"{base}/trips/3", 200)
        assert styled["display"] == {"clock": "24", "dates": "dmy", "maps": "osm"}
        assert [p["name"] for p in planned if "display" in p] == ["trip-24h-day-first"]
        names = [p["name"] for p in planned]
        # after every other page of the ledger: only the login pages come later
        assert names[names.index("trip-24h-day-first") + 1 :] == ["login", "login-wrong"]
        assert names.index("trip-3") < names.index("settings")
    monkeypatch.setattr(shoot, "fetch", lambda url, cookie="": (200, ""))
    assert "trip-24h-day-first" not in [p["name"] for p in shoot.plan_pages("http://web", "", 8)]


def test_the_browser_takes_the_page_it_photographs_as_a_secure_origin():
    """So it sends its own fetch metadata with a form post, as over https: the full Chromium,
    which honours the flag, told of each origin under test."""
    shoot = load_shoot()
    options = shoot.launch_options("http://web:8110")
    assert options["channel"] == "chromium"
    flags = [a for a in options["args"] if a.startswith("--unsafely-treat-insecure-origin")]
    assert flags == ["--unsafely-treat-insecure-origin-as-secure=http://web:8110"]
    assert "--disable-dev-shm-usage" in options["args"]
    assert not any("sec-fetch" in a.lower() for a in options["args"])
    both = shoot.launch_options("http://web:8110", "http://fresh:8110")["args"]
    flags = [a for a in both if a.startswith("--unsafely-treat-insecure-origin")]
    assert flags == ["--unsafely-treat-insecure-origin-as-secure=http://web:8110,http://fresh:8110"]


def test_a_brand_new_install_is_photographed_as_first_run(monkeypatch):
    """With the address of a second page over an empty ledger (`--fresh-url`, under the same
    base), its list is planned last, as `first-run`, at its own origin: the pages found from the
    first page keep their places, and so their files' names."""
    shoot = load_shoot()
    monkeypatch.setattr(shoot, "fetch", lambda url, cookie="": (200, ""))
    alone = shoot.plan_pages("http://web:8110", "/tripbox", 8, "tripbox_session=s1")
    planned = shoot.plan_pages(
        "http://web:8110", "/tripbox", 8, "tripbox_session=s1", fresh="http://fresh:8110/tripbox"
    )
    assert planned[:-1] == alone
    assert planned[-1] == {
        "name": "first-run",
        "path": "/tripbox/",
        "expect": 200,
        "origin": "http://fresh:8110",
    }
    for base in ("", "/a/b"):
        (last,) = shoot.plan_pages("http://web", base, 8, fresh=f"http://fresh:8110{base}")[-1:]
        assert (last["name"], last["path"]) == ("first-run", f"{base}/"), base
    assert "first-run" not in [p["name"] for p in alone]
    # the address of a page, from the origin it is served at
    assert shoot.page_url("http://web:8110", planned[0]) == "http://web:8110/tripbox/"
    assert shoot.page_url("http://web:8110", planned[-1]) == "http://fresh:8110/tripbox/"


class PostedPage:
    """A stand-in for a Playwright page showing the paste form, whose post is answered with each
    status of `answers` in turn: it lands on the status page when the last is a redirect, and
    otherwise stays, so the wait for the landing runs out."""

    def __init__(self, answers: list[int]) -> None:
        self.answers = answers
        self.listeners: list = []
        self.filled: tuple[str, str] | None = None
        self.clicked: list[str] = []

    def on(self, event, listener) -> None:
        assert event == "response"
        self.listeners.append(listener)

    def remove_listener(self, event, listener) -> None:
        self.listeners.remove(listener)

    def fill(self, selector: str, text: str) -> None:
        self.filled = (selector, text)

    def _answer(self, method: str, status: int) -> None:
        for listener in list(self.listeners):
            listener(SimpleNamespace(status=status, request=SimpleNamespace(method=method)))

    def click(self, selector: str) -> None:
        self.clicked.append(selector)
        for status in self.answers:
            self._answer("POST", status)
        if self.answers[-1:] == [303]:
            self._answer("GET", 200)

    @contextmanager
    def expect_navigation(self, *, url, wait_until, timeout):
        assert (url.pattern, wait_until) == (r"/uploads/[^/?#]+$", "load")
        landed = SimpleNamespace(value=None)
        yield landed
        if self.answers[-1:] != [303]:
            raise TimeoutError(f"Page.expect_navigation: Timeout {timeout}ms exceeded.")
        landed.value = SimpleNamespace(status=200, url="http://web:8110/tripbox/uploads/Ab9")


def test_a_paste_that_does_not_land_says_what_its_post_was_answered():
    shoot = load_shoot()
    page = PostedPage([303])
    assert shoot.send_paste(page, shoot.PASTE_LINE).status == 200
    assert page.filled == ('textarea[name="text"]', shoot.PASTE_LINE)
    assert page.clicked == ['form button[type="submit"]'] and page.listeners == []
    for answers, said in (
        ([403], "the paste form's post was answered 403, not sent on to its status page"),
        ([400], "the paste form's post was answered 400, not sent on to its status page"),
        ([], "the paste form's post got no answer"),
    ):
        page = PostedPage(answers)
        try:
            shoot.send_paste(page, shoot.PASTE_LINE, timeout_ms=1)
        except shoot.MissedLanding as missed:
            assert str(missed) == said, answers
            assert isinstance(missed.__cause__, TimeoutError)
        else:
            raise AssertionError(f"{answers} landed")
        assert page.listeners == [], answers
    assert shoot.missed_landing([303, 500]).startswith("the paste form's post was answered 500")


class LoginPage:
    """A stand-in for a Playwright page showing the login form, whose post is answered with
    `answer`, a response at `landed_at`."""

    def __init__(self, status: int, landed_at: str) -> None:
        self.answer = SimpleNamespace(status=status, url=landed_at)
        self.visited: list[str] = []
        self.filled: list[tuple[str, str]] = []
        self.clicked: list[str] = []

    def goto(self, url: str, wait_until: str):
        self.visited.append(url)

    def fill(self, selector: str, text: str) -> None:
        self.filled.append((selector, text))

    def click(self, selector: str) -> None:
        self.clicked.append(selector)

    @contextmanager
    def expect_navigation(self, *, wait_until, timeout):
        landed = SimpleNamespace(value=None)
        yield landed
        landed.value = self.answer


def test_a_browser_signs_in_through_the_login_form_once_per_context():
    shoot = load_shoot()
    login = "http://web:8110/tripbox/login"
    page = LoginPage(200, "http://web:8110/tripbox/")
    shoot.sign_in_form(page, login, "a-test-password")
    assert page.visited == [login]
    assert page.filled == [('input[name="password"]', "a-test-password")]
    assert page.clicked == ['form button[type="submit"]']
    for status, at in ((401, login), (429, login), (200, login), (500, "http://web:8110/tripbox/")):
        try:
            shoot.sign_in_form(LoginPage(status, at), login, "a-test-password")
        except shoot.NotSignedIn as refused:
            assert str(refused) == f"the login form was answered {status}"
        else:
            raise AssertionError(f"{status} at {at} signed in")
    wrong = LoginPage(401, login)
    assert shoot.send_password(wrong, shoot.WRONG_PASSWORD).status == 401
    assert wrong.filled == [('input[name="password"]', shoot.WRONG_PASSWORD)]


def test_the_session_cookie_is_read_from_the_sign_ins_answer():
    shoot = load_shoot()
    device = "tripbox_device=d1; HttpOnly; SameSite=Lax; Path=/tripbox; Max-Age=34560000"
    session = "tripbox_session=s-1_x; HttpOnly; SameSite=Lax; Path=/tripbox; Max-Age=15552000"
    secure = "__Secure-tripbox_session=s2; HttpOnly; SameSite=Lax; Path=/; Max-Age=0; Secure"
    assert shoot.session_cookie([device, session]) == "tripbox_session=s-1_x"
    assert shoot.session_cookie([secure]) == "__Secure-tripbox_session=s2"
    cleared = "tripbox_session=; HttpOnly; SameSite=Lax; Path=/; Max-Age=0"
    assert shoot.session_cookie([device, cleared]) == "" and shoot.session_cookie([]) == ""


def test_signed_in_the_pages_are_found_with_the_session_and_the_login_page_comes_last(
    monkeypatch,
):
    shoot = load_shoot()
    asked: list[tuple[str, str]] = []

    def fetch(url: str, cookie: str = "") -> tuple[int, str]:
        asked.append((url, cookie))
        return 200, '<a href="/tripbox/trips/1">a</a>' if url.endswith("/tripbox/") else ""

    monkeypatch.setattr(shoot, "fetch", fetch)
    planned = shoot.plan_pages("http://web", "/tripbox", 8, "tripbox_session=s1")
    assert asked and {cookie for _, cookie in asked} == {"tripbox_session=s1"}
    assert [p["name"] for p in planned][-6:] == [
        "deleted",
        "settings",
        "not-found",
        "trip-24h-day-first",
        "login",
        "login-wrong",
    ]
    login, wrong = planned[-2:]
    assert (login["path"], login["expect"], login["signed_out"]) == ("/tripbox/login", 200, True)
    assert (wrong["path"], wrong["expect"], wrong["signed_out"]) == ("/tripbox/login", 401, True)
    assert wrong["wrong"] == shoot.WRONG_PASSWORD and "wrong" not in login
    assert not any(p.get("signed_out") for p in planned[:-2])
    # with no login, there is no login page
    names = [p["name"] for p in shoot.plan_pages("http://web", "/tripbox", 8)]
    assert "login" not in names and "login-wrong" not in names


def test_file_names_sort_in_page_order():
    shoot = load_shoot()
    assert shoot.file_name(3, "trip-1", 390, "dark") == "03-trip-1-390-dark.png"
    names = [shoot.file_name(n, "page", 390, "light") for n in range(1, 13)]
    assert names == sorted(names)
    assert names[9].startswith("10-") and names[0].startswith("01-")


def test_only_requests_to_another_origin_are_blocked():
    shoot = load_shoot()
    base = "http://web:8110"
    assert not shoot.is_other_origin("http://web:8110/tripbox/static/app.css", base)
    assert not shoot.is_other_origin("HTTP://WEB:8110/tripbox/", base)
    assert shoot.is_other_origin("https://fonts.example/css", base)
    assert shoot.is_other_origin("http://web:9000/tripbox/", base)
    assert shoot.is_other_origin("http://web/tripbox/", base)
    assert shoot.is_other_origin("//cdn.example/x.js", base)
    assert not shoot.is_other_origin("//web:8110/x.js", base)
    for url in ("data:image/png;base64,AAAA", "about:blank", "blob:http://web:8110/1"):
        assert not shoot.is_other_origin(url, base)


def test_pages_are_planned_under_any_base(monkeypatch):
    """The page served at the host's root or under a base path: every page planned is under
    the base, the trips and edit pages found from the pages themselves."""
    shoot = load_shoot()
    for base in ("", "/tripbox", "/a/b"):
        pages = {
            f"{base}/": f'<a href="{base}/trips/1">a</a> <a href="/elsewhere/trips/2">b</a>',
            f"{base}/trips/1": f'<a href="{base}/items/5/edit" data-edit="car">c</a>',
        }
        def fetch(url: str, cookie: str = "", pages=pages) -> tuple[int, str]:
            return 200, pages.get(url.removeprefix("http://web"), "")

        monkeypatch.setattr(shoot, "fetch", fetch)
        planned = shoot.plan_pages("http://web", base, 8)
        assert all(p["path"].startswith(base + "/") for p in planned), base
        by_name = {p["name"]: p["path"] for p in planned}
        assert by_name["trip-1"] == f"{base}/trips/1" and "trip-2" not in by_name, base
        assert by_name["edit-car"] == f"{base}/items/5/edit", base
        assert by_name["index"] == f"{base}/" and by_name["add"] == f"{base}/add", base


def test_a_request_outside_the_base_is_counted():
    """A request to the page's own origin outside its base path is counted in the summary and
    fails the run; one under the base, to another origin or that leaves no page is not."""
    shoot = load_shoot()
    origin = "http://web:8110"
    for url in ("http://web:8110/static/app.css", "http://web:8110/", "//web:8110/trips/1"):
        assert shoot.escapes_base(url, origin, "/tripbox"), url
        assert not shoot.escapes_base(url, origin, ""), url
    for url in (
        "http://web:8110/tripbox/static/app.css",
        "http://web:8110/tripbox/",
        "https://fonts.example/css",
        "data:image/png;base64,AAAA",
    ):
        assert not shoot.escapes_base(url, origin, "/tripbox"), url
    assert shoot.escapes_base("http://web:8110/a/bc/x", origin, "/a/b")
    assert shoot.escapes_base("http://web:8110/a/b", origin, "/a/b")  # the bare base: not in it
    page = {"name": "index", "files": [], "overflow_px": 0, "other_origins": [], "unnamed": []}
    page["csp"] = [[]]  # one load read, nothing refused
    report = {
        "pages": [{**page, "outside_base": ["/static/app.css", "/"]}, {**page, "outside_base": []}],
        "overflow_pages": 0,
        "other_origin_hosts": [],
        "csp_probe": "reported",
    }
    report["checks"] = shoot.judged(
        report, ("trip-1", []), [], 2, ("shared", []), clean_seen()
    )
    assert shoot.summary(report)[-2:] == ["outside the base: 2 request(s)", "checks: 7 of 8 ok"]
    (outside,) = [c for c in report["checks"] if not c["ok"]]
    assert outside == {
        "name": "outside-the-base",
        "ok": False,
        "detail": "index: asked /static/app.css; index: asked /",
    }
    assert shoot.verdict(report, [], False) == "screens: FAIL check outside-the-base"
    assert shoot.verdict(report, ["trip-1"], False) == "screens: FAIL trip-1"
    report["pages"][0]["outside_base"] = []
    report["checks"] = shoot.judged(
        report, ("trip-1", []), [], 2, ("shared", []), clean_seen()
    )
    assert shoot.verdict(report, [], False) == "screens: ok"
    report["overflow_pages"] = 1
    assert shoot.verdict(report, [], False) == "screens: ok"
    assert shoot.verdict(report, [], True) == "screens: FAIL overflow"


def test_a_default_port_and_a_websocket_are_judged_by_origin():
    shoot = load_shoot()
    for url, origin in (
        ("http://web:80/x", "http://web"),
        ("http://web/x", "http://web:80"),
        ("https://web:443/x", "https://web"),
        ("HTTPS://WEB/x", "https://web:443"),
        ("//web/x", "http://web:80"),
    ):
        assert not shoot.is_other_origin(url, origin), (url, origin)
    for url, origin in (
        ("ws://web:8110/live", "http://web:8110"),
        ("wss://web:8110/live", "https://web:8110"),
        ("wss://sockets.example/live", "http://web:8110"),
        ("https://web/x", "http://web"),
        ("http://web:8443/x", "https://web:8443"),
        ("http://web:443/x", "https://web"),
    ):
        assert shoot.is_other_origin(url, origin), (url, origin)


def test_the_summary_counts_overflow_and_other_origins():
    shoot = load_shoot()
    report = {
        "pages": [
            {
                "name": "index",
                "files": ["a", "b"],
                "overflow_px": 0,
                "other_origins": [],
                "outside_base": [],
            },
            {
                "name": "x",
                "files": ["c"],
                "overflow_px": 12,
                "other_origins": ["h1.example"],
                "outside_base": [],
            },
            {
                "name": "y",
                "files": ["d", "e"],
                "overflow_px": 3,
                "other_origins": ["h1.example", "h2.example"],
                "outside_base": [],
            },
        ],
        "overflow_pages": 2,
        "other_origin_hosts": ["h1.example", "h2.example"],
        "checks": [{"name": n, "ok": n != "other-origins", "detail": ""} for n in NAMES],
    }
    lines = shoot.summary(report)
    assert lines == [
        "screens: 3 pages, 5 files",
        "overflow at 320: 2 page(s)",
        "other origins: 3 request(s) to 2 host(s)",
        "outside the base: 0 request(s)",
        "checks: 7 of 8 ok",
    ]
    assert shoot.verdict(report, [], False) == "screens: FAIL check other-origins"


def test_the_stack_publishes_nothing_and_mounts_only_its_own_directory():
    compose = yaml.safe_load((SCREENS / "compose.yml").read_text())
    services = compose["services"]
    assert set(services) == {"web", "fresh", "browser"}
    for svc in services.values():
        for key in ("ports", "network_mode", "container_name", "restart", "env_file", "privileged"):
            assert key not in svc, key
        for vol in svc.get("volumes", []):
            assert vol.split(":")[0].startswith("./")
    assert services["web"]["image"] == "${SCREENS_TRIPBOX_IMAGE:?}"
    assert services["browser"]["image"] == "${SCREENS_BROWSER_IMAGE:?}"
    assert services["web"]["read_only"] is True
    web, browser = services["web"], services["browser"]
    # served under the run's base path behind its own sign-in, whose password the browser has too
    assert web["environment"]["TRIPBOX_AUTH"] == "password"
    password = "${SCREENS_PASSWORD:-harness-only-password}"
    assert web["environment"]["TRIPBOX_PASSWORD"] == password
    assert browser["environment"]["SCREENS_PASSWORD"] == password
    assert web["environment"]["TRIPBOX_BASE_URL"] == "http://web:8110${SCREENS_BASE:-}"
    # every interface of its container, so the browser's container reaches it by name
    assert web["command"] == [
        "python", "-m", "tripbox", "web", "--host", "0.0.0.0", "--port", "8110"
    ]  # fmt: skip
    assert web["tmpfs"] == ["/tmp"]
    assert web["environment"]["TRIPBOX_DATA_DIR"] == "/demo/data"
    assert web["environment"]["TRIPBOX_CONFIG_DIR"] == "/demo/config"
    assert web["healthcheck"]["test"][0] == "CMD"
    assert browser["depends_on"]["web"]["condition"] == "service_healthy"
    assert set(browser["volumes"]) == {
        "./shoot.py:/harness/shoot.py:ro",
        "./checks.py:/harness/checks.py:ro",
        "./out:/out",
    }
    assert browser["environment"]["HOME"] == "/tmp"
    # the offline check's cut reaches the service worker's own requests (the check proves it)
    assert browser["environment"]["PW_EXPERIMENTAL_SERVICE_WORKER_NETWORK_EVENTS"] == "1"
    # a brand-new install: the same image over an empty data directory on its tmpfs, behind the
    # same sign-in under the same base, nothing mounted; the browser finds it by its address
    new = services["fresh"]
    assert new["image"] == web["image"] and new["command"] == web["command"]
    assert new["command"][-4:] == ["--host", "0.0.0.0", "--port", "8110"]
    assert new["read_only"] is True and new["tmpfs"] == ["/tmp"] and "volumes" not in new
    assert new["environment"]["TRIPBOX_AUTH"] == "password"
    assert new["environment"]["TRIPBOX_PASSWORD"] == password
    assert new["environment"]["TRIPBOX_BASE_URL"] == "http://fresh:8110${SCREENS_BASE:-}"
    for name in ("TRIPBOX_DATA_DIR", "TRIPBOX_CONFIG_DIR", "TRIPBOX_LOGS_DIR"):
        assert new["environment"][name].startswith("/tmp/"), name
    features = ("TRIPBOX_MAIL", "TRIPBOX_LLM", "TRIPBOX_AIRTRAIL", "TRIPBOX_TELEGRAM")
    assert not any(name.startswith(features) for name in new["environment"])
    assert new["healthcheck"] == web["healthcheck"]
    assert browser["depends_on"]["fresh"]["condition"] == "service_healthy"
    assert browser["environment"]["SCREENS_FRESH_URL"] == "http://fresh:8110${SCREENS_BASE:-}"
    # the browser image is built from a base pinned by digest, and adds only the Python package
    # of the same version, under a constraints file that pins what it pulls
    lines = [
        ln.strip()
        for ln in (SCREENS / "Dockerfile").read_text().splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]
    assert len(lines) == 3, lines
    m = re.fullmatch(r"FROM [a-z0-9./-]+:v(\d+\.\d+\.\d+)-[a-z]+@sha256:[0-9a-f]{64}", lines[0])
    assert m, lines[0]
    assert lines[1] == "COPY constraints.txt /constraints.txt"
    assert lines[2] == (
        f"RUN pip install --no-cache-dir -c /constraints.txt playwright=={m.group(1)}"
    )
    # and the constraints pin that same version: the three move together, by hand
    pins = (SCREENS / "constraints.txt").read_text().splitlines()
    assert f"playwright=={m.group(1)}" in pins


def test_the_harness_names_nothing_private():
    text = (SCREENS / "compose.yml").read_text() + (SCREENS / "shoot.py").read_text()
    text += (SCREENS / "checks.py").read_text()
    text += (SCREENS / "Dockerfile").read_text()
    readme = README.read_text()
    start = readme.index("screens/")
    text += readme[start:]
    for needle in ("tripboxoss", "tools/private", "/home/"):
        assert needle not in text, needle
    assert not re.search(r":183\d\d\b", text)


# ----------------------------------------------------------------------------- the checks
def a_page(name: str, **kw) -> dict:
    """A page's entry in report.json as the pictures leave it, nothing found unless `kw` says:
    `csp` holds what each load of it read of its violations (None: no listener on that load)."""
    entry = {"name": name, "files": [], "overflow_px": 0, "other_origins": [], "outside_base": []}
    return {**entry, "csp": [[], []], "unnamed": [], **kw}


def test_the_eight_checks_run_in_order_and_each_names_the_page_at_fault():
    shoot = load_shoot()
    checks = shoot.checks
    assert checks.NAMES == NAMES
    clean = {"pages": [a_page("index"), a_page("trip-3")], "csp_probe": "reported"}
    judged = shoot.judged(clean, ("trip-3", []), [], 2, ("shared", []), clean_seen())
    assert [c["name"] for c in judged] == list(NAMES) and all(c["ok"] for c in judged)
    assert all(set(c) == {"name", "ok", "detail"} and c["detail"] for c in judged)
    inline = {"directive": "style-src-attr", "blocked": "inline", "element": "div.cover"}
    font = {"directive": "font-src", "blocked": "https://fonts.example/a.woff2", "element": ""}
    pages = [
        a_page("index", csp=[[inline, inline], [font]], other_origins=["fonts.example"] * 2),
        a_page("trip-3", unnamed=['input[name="title"] in #trip-menu', "select.row"]),
        a_page("login", unnamed=None),  # it did not load
    ]
    report = {"pages": pages, "csp_probe": "reported"}
    judged = {
        c["name"]: c
        for c in shoot.judged(report, ("trip-3", []), [], 2, ("shared", []), clean_seen())
    }
    assert judged["csp"]["detail"] == (
        "index: style-src-attr refused inline at div.cover;"
        " index: font-src refused https://fonts.example/a.woff2"
    )
    assert judged["other-origins"]["detail"] == "index: asked fonts.example"
    assert judged["labels"]["detail"] == (
        'trip-3: input[name="title"] in #trip-menu has no name; trip-3: select.row has no name;'
        " login: not looked at"
    )
    assert [n for n, c in judged.items() if not c["ok"]] == ["csp", "other-origins", "labels"]
    # a long list names the first few and says how many more
    many = {"pages": [a_page(f"p{n}", outside_base=["/x"]) for n in range(11)]}
    many["csp_probe"] = "reported"
    run = shoot.judged(many, ("p0", []), [], 11, ("p1", []), clean_seen())
    (outside,) = [c for c in run if not c["ok"]]
    assert outside["detail"].endswith("p7: asked /x; and 3 more")
    assert outside["detail"].count("asked /x") == checks.SHOWN


class Sheets:
    """A stand-in for a Playwright page showing a trip with `n` ⋯ sheets, of which those in
    `broken` misbehave: "shut" (Enter does not open it), "stays" (Escape leaves it open) or
    "strays" (Escape closes it and the focus goes to the page)."""

    def __init__(self, n: int, broken: dict[int, str] | None = None) -> None:
        self.open = [False] * n
        self.focus: int | None = None
        self.broken = broken or {}
        self.keyboard = self
        self.pressed: list[str] = []

    def evaluate(self, script: str, arg=None):
        shoot = load_shoot()
        checks = shoot.checks
        if script == checks.MENU_COUNT:
            return len(self.open)
        if script == checks.MENU_WHERE:
            return f"the ⋯ of #item-{arg + 1}"
        if script == checks.MENU_FOCUS:
            self.focus = arg
            return True
        if script == checks.MENU_STATE:
            active = "body" if self.focus is None else f"summary in #item-{self.focus + 1}"
            return {"open": self.open[arg], "focused": self.focus == arg, "active": active}
        raise AssertionError(script)

    def press(self, key: str) -> None:
        self.pressed.append(key)
        how = self.broken.get(self.focus)
        if key == "Enter" and how != "shut":
            self.open[self.focus] = not self.open[self.focus]
        elif key == "Escape" and how != "stays":
            self.open = [False] * len(self.open)
            if how == "strays":
                self.focus = None


def test_every_sheet_of_a_trip_must_close_on_escape_and_give_the_focus_back():
    shoot = load_shoot()
    checks = shoot.checks
    good = Sheets(3)
    assert checks.escape_problems(good) == []
    assert good.pressed == ["Enter", "Escape"] * 3
    bad = Sheets(4, {1: "stays", 2: "strays", 3: "shut"})
    assert checks.escape_problems(bad) == [
        "the ⋯ of #item-2: Escape left its sheet open",
        "the ⋯ of #item-3: Escape left the focus on body",
        "the ⋯ of #item-4: Enter did not open its sheet",
    ]
    assert checks.escape_problems(Sheets(0)) == ["no ⋯ sheet on the page"]
    failed = checks.escape("trip-7", checks.escape_problems(bad))
    assert not failed["ok"] and failed["detail"].startswith("trip-7: the ⋯ of #item-2: Escape")
    assert checks.escape(None, [])["detail"] == "no trip page was planned"
    assert checks.escape("trip-7", [])["ok"]
    # the scripts look for the ⋯ sheets and hand the focus to the ⋯ itself
    for script in (checks.MENU_COUNT, checks.MENU_FOCUS, checks.MENU_STATE):
        assert "details.menu, details.item-menu" in script
    assert ":scope > summary" in checks.MENU_FOCUS


def test_a_page_that_does_not_land_on_the_login_page_is_named():
    shoot = load_shoot()
    checks = shoot.checks
    landed = {"index": "/a/login", "trip-1": "/a/trips/1", "deleted": "/a/login", "x": ""}
    assert checks.landing_problems(landed, "/a/login", "after sign-out") == [
        "trip-1 after sign-out landed on /a/trips/1",
        "x after sign-out landed on nothing",
    ]
    assert checks.landing_problems({"index": "/a/login"}, "/a/login", "with no cookie") == []
    ok = checks.protected([], 19)
    assert ok["ok"] and ok["detail"].startswith("19 page(s) land on the login page")
    assert checks.protected(["trip-1 with no cookie landed on /a/trips/1"], 19)["ok"] is False
    # no page loaded signed out: nothing was checked, which never passes
    assert checks.protected([], 0) == {
        "name": "protected",
        "ok": False,
        "detail": "no page was checked",
    }


def test_the_page_scripts_look_for_what_the_checks_name():
    """The init script keeps each violation of the content policy; the name finder opens every
    <details> and looks at the six kinds of control, hidden inputs and unseen ones aside."""
    checks = load_shoot().checks
    assert "securitypolicyviolation" in checks.CSP_LISTENER
    assert checks.CSP_LISTENER.strip().startswith("(() => {")
    assert "__tripboxViolations" in checks.CSP_READ
    unnamed = checks.UNNAMED
    assert 'querySelectorAll("details")) d.open = true' in unnamed
    assert 'querySelectorAll("input, select, textarea, button, summary, a[href]")' in unnamed
    for source in ("aria-labelledby", "aria-label", "el.labels", "title", "placeholder"):
        assert source in unnamed, source
    assert 'el.type === "hidden"' in unnamed and "checkVisibility" in unnamed


class Policy:
    """A stand-in for a Playwright page under the content policy: `listening` whether the init
    script installed its listener, `enforcing` whether the policy refuses an inline style (and
    the refusal is reported), `seen` the violations kept so far."""

    def __init__(self, *, listening: bool = True, enforcing: bool = True, seen=()) -> None:
        self.listening, self.enforcing, self.seen = listening, enforcing, list(seen)

    def evaluate(self, script: str, arg=None):
        checks = load_shoot().checks
        if script == checks.CSP_READ:
            return list(self.seen) if self.listening else None
        if script == checks.CSP_PROBE:
            if not self.listening:
                return "no listener"
            if not self.enforcing:
                return "silent"
            self.seen.clear()  # the planted refusal arrives, and the record is cleared
            return "reported"
        raise AssertionError(script)


def test_the_csp_check_proves_itself_and_never_trusts_a_page_it_could_not_read():
    """A load whose listener is missing leaves its page not looked at; and once a run the check
    plants an inline style on the first page and fails unless that refusal is reported."""
    shoot = load_shoot()
    checks = shoot.checks
    inline = {"directive": "style-src-attr", "blocked": "inline", "element": "div.cover"}
    deaf = Policy(listening=False)
    first = Policy(seen=[inline])
    pages = [
        a_page("index", csp=[checks.read_violations(first), checks.read_violations(deaf)]),
        a_page("trip-3", csp=[]),  # no load of it was read
        a_page("deleted", csp=[checks.read_violations(Policy())]),
    ]
    probe = checks.probe(first)
    assert probe == "reported" and first.seen == []
    report = {"pages": pages, "csp_probe": probe}
    csp = shoot.judged(report, ("trip-3", []), [], 3, ("shared", []), clean_seen())[0]
    assert csp == {
        "name": "csp",
        "ok": False,
        "detail": "index: not looked at; index: style-src-attr refused inline at div.cover;"
        " trip-3: not looked at",
    }
    planted = "the page's policy did not report a planted inline style"
    clean = [a_page("index"), a_page("deleted")]
    for answer in ("silent", "no listener", None):
        report = {"pages": clean, "csp_probe": answer}
        report["checks"] = shoot.judged(
            report, ("trip-3", []), [], 2, ("shared", []), clean_seen()
        )
        assert report["checks"][0] == {"name": "csp", "ok": False, "detail": planted}, answer
        assert shoot.failures(report) == [f"csp: {planted}"], answer
        assert shoot.verdict(report, [], False) == "screens: FAIL check csp"
    assert checks.probe(Policy(enforcing=False)) == "silent"
    assert checks.probe(Policy(listening=False)) == "no listener"
    report = {"pages": clean, "csp_probe": "reported"}
    report["checks"] = shoot.judged(
        report, ("trip-3", []), [], 2, ("shared", []), clean_seen()
    )
    assert report["checks"][0]["ok"] and shoot.failures(report) == []
    # what the page is asked: null without the listener; the planted style, then a clean record
    assert "null" in checks.CSP_READ
    assert 'setAttribute("style", "color: red")' in checks.CSP_PROBE
    assert "seen.length = 0" in checks.CSP_PROBE and checks.PROBE_ID in checks.CSP_PROBE


# ----------------------------------------------------------------------------- the shared page
SHARE_FORM = (
    '<form method="post" action="{trip}/links" class="col"><div class="toggles">'
    '<label><input type="checkbox" name="names" checked> Names</label>'
    '<label><input type="checkbox" name="codes"> Confirmation codes</label>'
    '<label><input type="checkbox" name="times" checked> Times</label>'
    "</div></form>"
)


def test_a_link_is_made_through_the_share_page_and_its_page_planned_signed_out(monkeypatch):
    """After the first trip's share page: a link made by posting that page's form as it stands
    (its ticked boxes, with the page's Origin and the session), and the shared page it then
    lists, loaded with no cookie; no form, a post not sent on, or no link listed: no such page."""
    shoot = load_shoot()
    for base in ("", "/a/b"):
        trip = f"{base}/trips/3"
        made: list[tuple] = []
        listed = f'<button data-copy="http://web{base}/s/Ab-_9xZ">Copy link</button>'

        def fetch(url: str, cookie: str = "", base=base, trip=trip, made=made, listed=listed):
            path = url.removeprefix("http://web")
            if path == f"{base}/":
                return 200, f'<a href="{trip}">a</a> <a href="{base}/trips/4">b</a>'
            if path == f"{trip}/share":
                return 200, SHARE_FORM.format(trip=trip) + (listed if made else "")
            return 200, ""

        def post(url, data, origin, cookie="", made=made):
            made.append((url, data, origin, cookie))
            return 303

        monkeypatch.setattr(shoot, "fetch", fetch)
        monkeypatch.setattr(shoot, "post_form", post)
        planned = shoot.plan_pages("http://web", base, 8, "tripbox_session=s1")
        posted = {"names": "on", "times": "on"}
        cookie = "tripbox_session=s1"
        assert made == [(f"http://web{trip}/links", posted, "http://web", cookie)]
        names = [p["name"] for p in planned]
        assert names[names.index("share") + 1] == "shared"
        (page,) = [p for p in planned if p["name"] == "shared"]
        assert page == {
            "name": "shared",
            "path": f"{base}/s/Ab-_9xZ",
            "expect": 200,
            "signed_out": True,
        }
    # a share page with no form, a post that is refused, a page that lists no link: none planned
    for html, answer in (("", 303), (SHARE_FORM.format(trip="/trips/3"), 403)):
        monkeypatch.setattr(
            shoot,
            "fetch",
            lambda url, cookie="", html=html: (
                200,
                '<a href="/trips/3">a</a>' if url.endswith("web/") else html,
            ),
        )
        monkeypatch.setattr(shoot, "post_form", lambda *a, answer=answer, **k: answer)
        assert "shared" not in [p["name"] for p in shoot.plan_pages("http://web", "", 8)]
    listed = '<b data-copy="https://x.example/b/s/Q_1-z">c</b>'
    assert shoot.shared_link(listed, "/b") == "/b/s/Q_1-z"
    assert shoot.shared_link('<b data-copy="https://x.example/s/Q">c</b>', "/b") == ""
    assert len(shoot.WRONG_LINK) == 32 and re.fullmatch(r"[A-Za-z0-9_-]+", shoot.WRONG_LINK)


def test_the_shared_check_names_a_script_a_request_and_a_wrong_link_that_is_not_bare():
    checks = load_shoot().checks
    path = "/b/s/Ab-_9xZ"
    asked = [
        path,
        "/b/static/app.css?v=1a2b",
        "/b/static/fonts/PublicSans-Regular.woff2",
        "/b/static/favicon.svg",
    ]
    clean = a_page("shared", path=path, asked=asked, scripts=[0, 0, 0, 0, 0])
    stores = {"tripbox/b:pages:abc": ["http://web:8110/b/"]}
    assert checks.shared_problems(clean, "/b", (404, b""), stores) == []
    ok = checks.shared("shared", [])
    assert ok["ok"] and ok["detail"].startswith("shared ran no script")
    dirty = a_page(
        "shared",
        path=path,
        asked=[*asked, "/b/static/app.js?v=1a2b", "/favicon.ico", "/b/trips/3"],
        scripts=[0, 1, None],
    )
    kept = {"tripbox/b:pages:abc": ["http://web:8110" + path]}
    assert checks.shared_problems(dirty, "/b", (200, b"<p>a page</p>"), kept) == [
        "shared: not looked at",
        "shared: holds 1 script element(s)",
        "shared: asked /b/static/app.js?v=1a2b",
        "shared: asked /favicon.ico",
        "shared: asked /b/trips/3",
        "shared: a store holds it",
        "a wrong link was answered 200",
        "a wrong link's answer holds 13 byte(s)",
    ]
    # a font under another directory, or a stylesheet outside the base, is not the page's own
    stray = a_page(
        "shared",
        path=path,
        asked=["/static/app.css", "/b/static/fonts/x/y.woff2", "/static/favicon.svg"],
    )
    assert checks.shared_problems({**stray, "scripts": [0]}, "/b", (404, b""), {}) == [
        "shared: asked /static/app.css",
        "shared: asked /b/static/fonts/x/y.woff2",
        "shared: asked /static/favicon.svg",
    ]
    # the stores not read once it was opened signed in: not looked at
    assert checks.shared_problems(clean, "/b", (404, b"")) == [
        "shared: the stores were not read once it was opened signed in"
    ]
    failed = checks.shared("shared", ["shared: asked /b/trips/3"])
    assert failed == {"name": "shared", "ok": False, "detail": "shared: asked /b/trips/3"}
    assert checks.shared(None, [])["detail"] == "no shared page was planned"
    assert checks.SCRIPTS == "() => document.scripts.length"


# ----------------------------------------------------------------------------- offline
ORIGIN = "http://web:8110"
SAVED_AT = "when the copy was saved"  # the worker writes a time; the check asks only that it is


def clean_seen(base: str = "/b") -> dict:
    """What the offline check sees when every step holds, at `base`."""
    checks = load_shoot().checks
    trip = base + "/trips/3"
    static = {f"tripbox{base}:static:abc": [ORIGIN + base + "/static/app.css?v=abc"]}
    return {
        "base_path": base,
        "want_scope": ORIGIN + base + "/",
        "untested": None,
        "scope": ORIGIN + base + "/",
        "manifest": {"url": ORIGIN + base + "/manifest.webmanifest", "errors": [], "data": "{}"},
        "title": "Sample trip",
        "kept": [base + "/", trip],
        "stored": {f"tripbox{base}:pages:abc": [ORIGIN + base + "/", ORIGIN + trip], **static},
        "copy": {
            "status": 200,
            "saved": SAVED_AT,
            "banner": "Saved copy · 5 min ago. You are offline, so nothing can be changed here.",
            "title": "Sample trip",
            "text": "Sample trip",
            "enabled": [],
            "menus": [],
        },
        "no_copy": {"status": 503, "text": "Trips " + checks.NO_COPY},
        "refused": {"status": 503, "text": "Trips " + checks.NOT_SAVED},
        "after_stored": {f"tripbox{base}:pages:abc": [], **static},
        "after": {"status": 503, "text": "Trips " + checks.NO_COPY, "saved": None, "error": None},
        "list_kept": True,
        "list_title": "Trips",
        "list_copy": {
            "status": 200,
            "saved": SAVED_AT,
            "banner": "Saved copy · just now. You are offline, so nothing can be changed here.",
            "title": "Trips",
            "text": "Trips",
            "enabled": [],
            "menus": [],
        },
    }


def test_the_offline_check_passes_only_when_every_step_holds():
    checks = load_shoot().checks
    for base in ("", "/b", "/a/b"):
        seen = clean_seen(base)
        assert checks.offline_problems(seen) == [], base
        ok = checks.offline(seen, [])
        assert ok["ok"] and ok["detail"].startswith("the worker is scoped to the base")
    # every age the banner may say
    for age in ("just now", "1 min ago", "59 min ago", "3 h ago", "12 d ago"):
        banner = f"Saved copy · {age}. You are offline, so nothing can be changed here."
        assert checks.BANNER.fullmatch(banner), age
    assert not checks.BANNER.fullmatch("Saved copy · soon. You are offline, so nothing can be")
    # at the root a sign-out clears the site, worker and all: a load that fails is no copy
    seen = clean_seen("")
    seen["after"] = {"saved": None, "error": "Error"}
    assert checks.offline_problems(seen) == []
    # under a sub-path the worker stays and must answer: a load that fails is a problem
    for base in ("/b", "/a/b"):
        seen = clean_seen(base)
        seen["after"] = {"saved": None, "error": "Error"}
        assert checks.offline_problems(seen) == [
            "(5) after sign-out the trip did not load (Error)"
        ], base


def test_the_offline_check_reports_what_it_could_not_test_and_never_passes_it():
    checks = load_shoot().checks
    seen = {**clean_seen(), "untested": "cutting the network reached no request the service"
            " worker made"}
    assert checks.offline(seen, []) == {
        "name": "offline",
        "ok": False,
        "detail": "could not test: cutting the network reached no request the service worker"
        " made",
    }
    assert checks.offline(None, []) == {
        "name": "offline",
        "ok": False,
        "detail": "could not test: the check did not run",
    }
    # judged hands what was seen to the check
    shoot = load_shoot()
    report = {"pages": [a_page("index")], "csp_probe": "reported"}
    run = shoot.judged(report, ("trip-3", []), [], 1, ("shared", []), seen)
    assert run[-1]["name"] == "offline" and not run[-1]["ok"]
    assert run[-1]["detail"].startswith("could not test: ")


def test_the_offline_check_names_each_step_that_fails():
    checks = load_shoot().checks
    base = "/b"
    seen = clean_seen(base)
    seen["scope"] = ORIGIN + "/"
    seen["manifest"] = {"url": "", "errors": [{"message": "icon too small", "critical": False}]}
    seen["copy"] = {
        **seen["copy"],
        "status": 503,
        "saved": None,
        "title": "Other",
        "banner": None,
        "enabled": ['input[name="title"]', 'input[name="title"]'],
        "menus": ["the ⋯ of #item-4", "Add notes"],
        "links": ["the share link"],
    }
    seen["no_copy"] = {"status": 200, "text": "a page"}
    seen["refused"] = None
    seen["stored"] = {
        f"tripbox{base}:pages:abc": [
            ORIGIN + base + "/",
            ORIGIN + base + "/login",
            ORIGIN + base + "/s/Ab-_9x",
            ORIGIN + base + "/settings",
            ORIGIN + base + "/trips/3/share",
        ],
        f"tripbox{base}:static:abc": [ORIGIN + base + "/add/paste"],
    }
    seen["after"] = {"status": 200, "text": "Sample trip", "saved": SAVED_AT}
    seen["after_stored"] = {f"tripbox{base}:pages:abc": [ORIGIN + base + "/trips/3"]}
    assert checks.offline_problems(seen) == [
        f"(1) the worker's scope is {ORIGIN}/, not {ORIGIN}{base}/",
        "(2) the trip reloaded offline was answered 503, not its copy",
        "(2) the saved copy's title is 'Other', not 'Sample trip'",
        "(2) the saved copy's banner says None",
        '(2) input[name="title"] is not disabled',
        "(2) the ⋯ of #item-4 is not disabled",
        "(2) Add notes is not disabled",
        "(2) the share link is not disabled",
        "(3) a trip never opened was answered 200, not 'You are offline and this page has no sav'…",
        "(3) a form posted from the saved copy was not tried",
        "(4) a store holds /b/login",
        "(4) a store holds /b/s/Ab-_9x",
        "(4) a store holds /b/settings",
        "(4) a store holds /b/add/paste",
        "(4) no store holds /b/trips/3",
        "(4) the pages store holds /b/login",
        "(4) the pages store holds /b/s/Ab-_9x",
        "(4) the pages store holds /b/settings",
        "(4) the pages store holds /b/trips/3/share",
        "(5) after sign-out the trip opened as a saved copy",
        f"(5) after sign-out the pages store holds {ORIGIN}{base}/trips/3",
        "(6) Chromium found no manifest",
        "(6) icon too small",
    ]
    # (7) the list the worker was installed on: never kept, so offline it has no copy
    landed = clean_seen(base)
    landed["list_kept"] = False
    landed["list_copy"] = {
        "status": 503,
        "text": "Trips " + checks.NO_COPY,
        "saved": None,
        "title": "Trips",
        "banner": None,
    }
    assert checks.offline_problems(landed) == [
        "(7) no store held the list within 10 s of the worker taking control",
        "(7) the list reloaded offline was answered 503, not its copy",
        "(7) the list's saved copy's banner says None",
    ]
    landed["list_copy"] = {**clean_seen(base)["list_copy"], "title": "Sample trip"}
    landed["list_kept"] = True
    assert checks.offline_problems(landed) == [
        "(7) the list's saved copy's title is 'Sample trip', not 'Trips'"
    ]
    del landed["list_copy"]
    assert checks.offline_problems(landed) == [
        "(7) the list sign-in landed on was not reloaded offline"
    ]
    # a path that only begins like a page that is never kept is not one
    near = clean_seen(base)
    near["stored"][f"tripbox{base}:static:abc"].append(ORIGIN + base + "/static/loginx.css")
    near["stored"][f"tripbox{base}:static:abc"].append(ORIGIN + base + "/addresses")
    assert checks.offline_problems(near) == []
    # a step that could not be driven is said first, and what it never reached after it
    broken = {"base_path": base, "want_scope": ORIGIN + base + "/", "error": "TimeoutError: x"}
    problems = checks.offline_problems(broken)
    assert problems[0] == "TimeoutError: x"
    assert "(1) no service worker is registered" in problems
    assert "(2) the trip was not reloaded offline" in problems
    assert "(5) the trip was not loaded offline after sign-out" in problems
    assert "(7) the list sign-in landed on was not reloaded offline" in problems
    assert "(6) the manifest was not asked for" in problems
    failed = checks.offline(broken, problems)
    assert not failed["ok"] and failed["detail"].startswith("TimeoutError: x; (1)")


def test_the_offline_check_opens_a_planned_trip_and_one_it_never_opened():
    shoot = load_shoot()
    pages = [
        {"name": "index", "path": "/b/"},
        {"name": "trip-3", "path": "/b/trips/3"},
        {"name": "trip-5", "path": "/b/trips/5"},
        {"name": "trip-24h-day-first", "path": "/b/trips/3", "display": {"clock": "24"}},
    ]
    assert shoot.offline_trips(pages, "/b") == ("/b/trips/3", "/b/trips/5")
    assert shoot.offline_trips(pages[:2], "/b") == ("/b/trips/3", "/b" + shoot.MISSING_TRIP)
    assert shoot.offline_trips(pages[:1], "/b") is None


def test_the_offline_scripts_look_for_what_the_page_names():
    """The scripts the offline check runs in the page ask for what the page's own code sets:
    the banner's class and role (app.js), the stores' names (sw.js), the ⋯ sheets."""
    checks = load_shoot().checks
    web = SCREENS.parents[2] / "src" / "tripbox" / "web"
    app_js = (web / "static" / "app.js").read_text()
    worker = (web / "templates" / "sw.js").read_text()
    assert 'banner.className = "saved-banner"' in app_js
    assert 'banner.setAttribute("role", "status")' in app_js
    assert '.saved-banner[role="status"]' in checks.SHOWN_NOW
    assert 'aria-disabled", "true"' in app_js and 'aria-disabled") !== "true"' in checks.SHOWN_NOW
    assert repr(checks.LOCKED) in checks.SHOWN_NOW
    assert checks.LOCKED == checks.MENUS + ", details.inline"
    assert 'const LOCKED = MENUS + ", details.inline";' in app_js
    # a copy button stays enabled on a copy, the share link is marked: as app.js does
    assert '!el.matches("[data-copy]")' in checks.SHOWN_NOW
    assert 'if (!el.matches("[data-copy]")) el.disabled = true;' in app_js
    assert 'querySelectorAll("a[data-share]")' in checks.SHOWN_NOW and "a[data-share]" in app_js
    assert 'PREFIX + "pages:" + VERSION' in worker
    for words in (checks.NO_COPY, checks.NOT_SAVED):
        assert words in worker.replace('"\n  "', ""), words
    assert "caches.keys()" in checks.STORED and "navigator.serviceWorker.ready" in (
        checks.WORKER_READY
    )
    # the list is waited for in a pages store, as the worker names it, for 10 s at most
    assert 'name.includes(":pages:")' in checks.KEPT_SOON and "match(url)" in checks.KEPT_SOON
    assert "waited <= 10000" in checks.KEPT_SOON and "return false;" in checks.KEPT_SOON
    assert checks.CUT == "internetdisconnected"
