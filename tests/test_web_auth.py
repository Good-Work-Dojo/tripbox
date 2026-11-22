# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
import datetime as dt
import inspect
import json
import logging

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from helpers.web import PEER, Page, client_for, raw_get
from jwt import PyJWKClient
from jwt.algorithms import RSAAlgorithm
from starlette.datastructures import Headers
from test_run import make_ctx
from test_web_views import seed_trip

from tripbox.config import ConfigError, Settings
from tripbox.ledger import Ledger
from tripbox.trips import ical
from tripbox.web import auth
from tripbox.web.app import create_app
from tripbox.web.middleware import SECURITY_HEADERS

TEAM, AUD, ME = "sample-family", "aud-123", "alex@example.com"
ISS = f"https://{TEAM}.cloudflareaccess.com"


class Keys:
    def __init__(self, kid="k1"):
        self.kid = kid
        self.private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.pem = self.private.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        jwk = json.loads(RSAAlgorithm.to_jwk(self.private.public_key()))
        jwk.update({"kid": kid, "alg": "RS256", "use": "sig"})
        self.jwks = {"keys": [jwk], "public_certs": []}

    def token(
        self,
        *,
        aud=AUD,
        iss=ISS,
        email=ME,
        kid=None,
        exp_minutes=60,
        iat_offset=0,
        nbf_offset=None,
        drop=(),
        key=None,
        algorithm="RS256",
    ):
        now = dt.datetime.now(dt.UTC)
        claims = {
            "aud": [aud],
            "iss": iss,
            "email": email,
            "iat": now + dt.timedelta(seconds=iat_offset),
            "exp": now + dt.timedelta(minutes=exp_minutes),
            "sub": "user",
        }
        if nbf_offset is not None:
            claims["nbf"] = now + dt.timedelta(seconds=nbf_offset)
        for k in drop:
            claims.pop(k, None)
        return jwt.encode(
            claims,
            self.pem if key is None else key,
            algorithm=algorithm,
            headers={"kid": kid or self.kid},
        )


@pytest.fixture
def keys(monkeypatch):
    k = Keys()
    k.fetches = 0

    def fake_fetch(self):
        k.fetches += 1
        return k.jwks

    monkeypatch.setattr(PyJWKClient, "fetch_data", fake_fetch)
    return k


PUBLIC = "https://trips.example.com"
# The Access login of these tests' page
ACCESS = {"TRIPBOX_ACCESS_TEAM": TEAM, "TRIPBOX_ACCESS_AUD": AUD, "TRIPBOX_ALLOWED_EMAILS": ME}
# The login proxy of these tests' page: it connects from the test requests' own peer and may
# sign in Alex alone, whom it names in the default header
PROXY = {"TRIPBOX_TRUSTED_PROXIES": PEER[0], "TRIPBOX_PROXY_USERS": "alex"}
SIGNED = {"Remote-User": "alex"}
# Peers the page behind that proxy does not trust: another address, in IPv6 and as IPv4-mapped
# IPv6, this computer's IPv6 address, a test client's name, and no peer at all
STRANGERS = (
    ("198.51.100.7", 40000),
    ("2001:db8::7", 40000),
    ("::ffff:198.51.100.7", 40000),
    ("::1", 40000),
    ("testclient", 50000),
    None,
)


def behind_access(tmp_path, monkeypatch, base):
    """The page behind the Access login at its public address, served under `base`, and its
    context."""
    env = {**ACCESS, "TRIPBOX_BASE_URL": PUBLIC + base}
    return client_for(tmp_path, monkeypatch, base=base, auth="cloudflare", env=env)


def test_requests_without_a_valid_access_token_are_refused(tmp_path, monkeypatch, base, keys):
    c, _ = behind_access(tmp_path, monkeypatch, base)
    assert c.get("/").status_code == 401
    ok = c.get("/", headers={"Cf-Access-Jwt-Assertion": keys.token()})
    assert ok.status_code == 200
    assert (
        c.get("/", headers={"Cf-Access-Jwt-Assertion": keys.token(aud="other")}).status_code
        == 401
    )
    assert (
        c.get(
            "/", headers={"Cf-Access-Jwt-Assertion": keys.token(iss="https://evil.example")}
        ).status_code
        == 401
    )
    assert (
        c.get(
            "/", headers={"Cf-Access-Jwt-Assertion": keys.token(exp_minutes=-5)}
        ).status_code
        == 401
    )
    assert (
        c.get(
            "/", headers={"Cf-Access-Jwt-Assertion": keys.token(email="x@y.z")}
        ).status_code
        == 401
    )
    assert c.get("/", headers={"Cf-Access-Jwt-Assertion": "garbage"}).status_code == 401


def test_unknown_kid_refetches_the_jwks_once_then_refuses(tmp_path, monkeypatch, base, keys):
    c, _ = behind_access(tmp_path, monkeypatch, base)
    assert c.get("/", headers={"Cf-Access-Jwt-Assertion": keys.token()}).status_code == 200
    before = keys.fetches
    r = c.get("/", headers={"Cf-Access-Jwt-Assertion": keys.token(kid="rotated")})
    assert r.status_code == 401 and keys.fetches == before + 1


def test_healthz_and_static_are_open_but_pages_are_not(tmp_path, monkeypatch, base, keys):
    c, _ = behind_access(tmp_path, monkeypatch, base)
    assert c.get("/healthz").text == "ok"
    assert c.get("/static/app.css").status_code == 200
    assert c.get("/trips/1").status_code == 401


def test_posts_need_a_same_origin_fetch(tmp_path, monkeypatch, base, keys):
    c, _ = behind_access(tmp_path, monkeypatch, base)
    auth = {"Cf-Access-Jwt-Assertion": keys.token()}
    cross = c.post(
        "/trips/1/rename",
        data={"title": "x"},
        headers={**auth, "Sec-Fetch-Site": "cross-site"},
    )
    assert cross.status_code == 403
    assert (
        c.post(
            "/trips/1/rename",
            data={"title": "x"},
            headers={**auth, "Sec-Fetch-Site": "same-origin"},
        ).status_code
        != 403
    )
    assert (
        c.post(
            "/trips/1/rename",
            data={"title": "x"},
            headers={**auth, "Origin": "https://trips.example.com"},
        ).status_code
        != 403
    )
    assert (
        c.post(
            "/trips/1/rename",
            data={"title": "x"},
            headers={**auth, "Origin": "https://evil.example"},
        ).status_code
        == 403
    )
    assert (
        c.post("/trips/1/rename", data={"title": "x"}, headers=auth).status_code == 403
    )  # no fetch metadata, no origin


def test_auth_none_is_refused_for_a_public_origin_and_nothing_skips_a_login(
    tmp_path, monkeypatch, base
):
    public = {"TRIPBOX_BASE_URL": PUBLIC + base}
    with pytest.raises(ConfigError, match="TRIPBOX_AUTH=none"):
        client_for(tmp_path, monkeypatch, base=base, env=public)
    local = {"TRIPBOX_BASE_URL": "http://localhost:8110" + base}
    c, _ = client_for(tmp_path, monkeypatch, base=base, env=local)
    assert c.get("/").status_code == 200
    # the page is built with its login, or not at all: no parameter serves it without one
    ctx, _ = make_ctx(tmp_path, monkeypatch, [], env={"TRIPBOX_AUTH": "cloudflare", **public})
    s = ctx.settings
    assert list(inspect.signature(create_app).parameters) == [
        "settings",
        "ledger_factory",
        "owns_ledger",
    ]
    with pytest.raises(ConfigError, match="TRIPBOX_ACCESS_TEAM"):
        create_app(s, lambda: Ledger.open(s.ledger_path), owns_ledger=True)


def test_feed_is_open_but_token_checked(tmp_path, monkeypatch, base, keys):
    from tripbox.trips import ical

    c, ctx = behind_access(tmp_path, monkeypatch, base)
    token = ical.feed_token(ctx.ledger)
    assert c.get(f"/feed/{token}.ics").status_code == 200  # no JWT needed
    assert c.get("/feed/nope.ics").status_code == 404
    assert c.get("/").status_code == 401


def test_token_algorithm_and_signature_tricks_are_refused(tmp_path, monkeypatch, base, keys):
    from cryptography.hazmat.primitives import serialization

    c, _ = behind_access(tmp_path, monkeypatch, base)
    H = "Cf-Access-Jwt-Assertion"
    assert c.get("/", headers={H: keys.token()}).status_code == 200
    public_pem = keys.private.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    unsigned = jwt.encode(
        {"aud": [AUD], "iss": ISS, "email": ME, "iat": 1, "exp": 4102444800},
        key=None,
        algorithm="none",
        headers={"kid": keys.kid},
    )
    assert c.get("/", headers={H: unsigned}).status_code == 401
    import base64
    import hashlib
    import hmac

    def b64(b: bytes) -> str:
        return base64.urlsafe_b64encode(b).rstrip(b"=").decode()

    body = {"aud": [AUD], "iss": ISS, "email": ME, "iat": 1, "exp": 4102444800}
    signing_input = (
        b64(json.dumps({"alg": "HS256", "typ": "JWT", "kid": keys.kid}).encode())
        + "."
        + b64(json.dumps(body).encode())
    )
    sig = hmac.new(public_pem, signing_input.encode(), hashlib.sha256).digest()
    confused = f"{signing_input}.{b64(sig)}"  # HS256 signed with the public key as the secret
    assert c.get("/", headers={H: confused}).status_code == 401
    forged = Keys(kid=keys.kid).token()  # another key pair, the known kid
    assert c.get("/", headers={H: forged}).status_code == 401
    assert c.get("/", headers={H: keys.token(nbf_offset=600)}).status_code == 401
    assert c.get("/", headers={H: keys.token(drop=("exp",))}).status_code == 401
    # a little clock skew is fine (the host is NTP-synced; Cloudflare's clock may be a second out)
    assert c.get("/", headers={H: keys.token(iat_offset=30)}).status_code == 200


def test_exempt_prefixes_are_exact_and_the_feed_never_redirects(tmp_path, monkeypatch, base, keys):
    c, _ = behind_access(tmp_path, monkeypatch, base)
    assert c.get("/healthzX").status_code == 401
    r = c.get("/feed/x.ics/", follow_redirects=False)
    assert r.status_code == 404 and "location" not in r.headers
    if base:  # the bare base goes on to its slash
        auth = {"Cf-Access-Jwt-Assertion": keys.token()}
        r = c.get("", headers=auth, follow_redirects=False)
        assert r.status_code in (301, 307, 308) and r.headers["location"] == c.url("/")
    r = c.post(
        "/trips/1/rename",
        data={"title": "x"},
        headers={"Sec-Fetch-Site": "same-site", "Origin": "https://other.example.com"},
    )
    assert r.status_code == 403  # the same-origin rule comes before the login
    r = c.post(
        "/trips/1/rename",
        data={"title": "x"},
        headers={
            "Cf-Access-Jwt-Assertion": keys.token(),
            "Sec-Fetch-Site": "same-site",
            "Origin": "https://other.example.com",
        },
    )
    assert r.status_code == 403  # a sibling subdomain is not our page


def test_stale_signing_keys_keep_working_while_the_jwks_fetch_fails(keys, monkeypatch):
    from tripbox.web.auth import AccessJwtVerifier, AuthError

    v = AccessJwtVerifier(TEAM, AUD, (ME,))
    assert v.verify(keys.token())["email"] == ME
    v.jwks.jwk_set_cache = None  # the hour is up: the next verify wants fresh keys

    def down(self):
        raise jwt.PyJWKClientConnectionError("dns")

    monkeypatch.setattr(PyJWKClient, "fetch_data", down)
    assert v.verify(keys.token())["email"] == ME  # last good keys still serve
    try:
        v.verify(Keys(kid="unknown").token())
    except AuthError:
        pass
    else:
        raise AssertionError("an unknown kid with no reachable JWKS must fail")


# ----------------------------------------------------------------------------- behind a login proxy
def behind_proxy(tmp_path, monkeypatch, base, **env):
    """The page behind the login proxy at its public address, served under `base`, and its
    context; `env` changes its settings."""
    page_env = {**PROXY, "TRIPBOX_BASE_URL": PUBLIC + base, **env}
    return client_for(tmp_path, monkeypatch, base=base, auth="proxy", env=page_env)


def refused_as(caplog) -> list[str]:
    """What the login logged of each request it refused."""
    return [r.getMessage() for r in caplog.records if r.name == "tripbox.web.auth"]


def refusal(word: str) -> str:
    return f"proxy sign-in refused: {word}"


def test_a_user_header_from_an_untrusted_peer_is_ignored(tmp_path, monkeypatch, base, caplog):
    """The header is believed only on a connection from the proxy itself: from any other peer it
    is ignored, whatever it says and whatever a forwarded header claims of where the request
    came from. Each refusal is one INFO line naming its reason, never the header's value or the
    peer."""
    c, _ = behind_proxy(tmp_path, monkeypatch, base)
    caplog.set_level(logging.INFO, logger="tripbox.web.auth")
    home = f"{base}/".encode()
    assert raw_get(c.app, home, list(SIGNED.items()))[0] == 200  # from the proxy
    assert refused_as(caplog) == []
    claims = [
        ("x-forwarded-for", PEER[0]),
        ("x-real-ip", PEER[0]),
        ("forwarded", f"for={PEER[0]}"),
    ]
    for peer in STRANGERS:
        for headers in (list(SIGNED.items()), [*SIGNED.items(), *claims]):
            status, _, body = raw_get(c.app, home, headers, client=peer)
            assert (status, body) == (401, b"unauthorized"), (peer, headers)
    assert refused_as(caplog) == [refusal("untrusted_peer")] * (2 * len(STRANGERS))
    for part in ("alex", "198.51", "2001:db8", "testclient"):
        assert not any(part in line for line in caplog.messages), part
    stranger = Page(c.app, base, headers=SIGNED, client=STRANGERS[0])
    assert stranger.get("/").status_code == 401
    assert Page(c.app, base, headers=SIGNED).get("/").status_code == 200


def test_a_trusted_proxys_listed_user_is_signed_in_and_an_unlisted_one_is_not(
    tmp_path, monkeypatch, base, caplog
):
    """From the proxy, the header names a user listed in the settings (case aside, trimmed), or
    nobody is signed in; the header is the one the settings name."""
    c, ctx = behind_proxy(tmp_path, monkeypatch, base)
    caplog.set_level(logging.INFO, logger="tripbox.web.auth")
    home = f"{base}/".encode()
    for value in ("alex", "ALEX", "Alex", " alex\t"):
        assert raw_get(c.app, home, [("Remote-User", value)])[0] == 200, value
    assert refused_as(caplog) == []
    unknown = ("sam", "", "alex,sam", "alex sam", "alexx", "xalex", ME, "\xe5lex")
    for value in unknown:
        status, _, body = raw_get(c.app, home, [("Remote-User", value)])
        assert (status, body) == (401, b"unauthorized"), value
    unnamed = ([], [("X-Remote-User", "alex")], [("Remote_User", "alex")], [("Remote", "alex")])
    for headers in unnamed:
        assert raw_get(c.app, home, headers)[0] == 401, headers
    words = [refusal("unknown_user")] * len(unknown) + [refusal("no_header")] * len(unnamed)
    assert refused_as(caplog) == words
    assert not any("sam" in line or "alex" in line for line in caplog.messages)
    # a signed-in post goes through, as from any login
    trip = seed_trip(ctx)
    signed = Page(c.app, base, headers={**SIGNED, "Sec-Fetch-Site": "same-origin"})
    r = signed.post(f"/trips/{trip.id}/notes", data={"notes": "kept"}, follow_redirects=False)
    assert r.status_code == 303 and ctx.trips.get(trip.id).notes == "kept"
    # another header, named in the settings
    other, _ = behind_proxy(
        tmp_path / "other", monkeypatch, base, TRIPBOX_PROXY_USER_HEADER="X-Auth-Request-User"
    )
    assert raw_get(other.app, home, list(SIGNED.items()))[0] == 401
    assert raw_get(other.app, home, [("X-Auth-Request-User", "alex")])[0] == 200


def test_a_doubled_user_header_is_refused(tmp_path, monkeypatch, base, caplog):
    """A proxy that adds its header beside one the browser sent leaves two: neither is
    believed, whichever names whom."""
    c, _ = behind_proxy(tmp_path, monkeypatch, base)
    caplog.set_level(logging.INFO, logger="tripbox.web.auth")
    home = f"{base}/".encode()
    doubled = (
        [("Remote-User", "alex"), ("Remote-User", "alex")],
        [("Remote-User", "alex"), ("Remote-User", "sam")],
        [("Remote-User", "sam"), ("Remote-User", "alex")],
        [("Remote-User", "alex"), ("Remote-User", "")],
        [("Remote-User", "alex"), ("remote-user", "alex")],
        [("Remote-User", "alex")] * 3,
    )
    for headers in doubled:
        status, _, body = raw_get(c.app, home, headers)
        assert (status, body) == (401, b"unauthorized"), headers
    assert refused_as(caplog) == [refusal("two_headers")] * len(doubled)
    assert raw_get(c.app, home, list(SIGNED.items()))[0] == 200


def test_a_peer_is_matched_as_an_address_not_as_text(tmp_path, monkeypatch, base):
    """The peer is read as an address (an IPv4-mapped IPv6 one as the IPv4 address it maps) and
    looked for in the trusted networks: text that begins like a trusted address is not one."""
    trusted = {"TRIPBOX_TRUSTED_PROXIES": "192.0.2.1/32, 2001:db8::/32"}
    c, _ = behind_proxy(tmp_path, monkeypatch, base, **trusted)
    home = f"{base}/".encode()

    def answer(host: str) -> int:
        return raw_get(c.app, home, list(SIGNED.items()), client=(host, 40000))[0]

    for host in ("192.0.2.1", "::ffff:192.0.2.1", "2001:db8::7", "2001:DB8::7", "2001:db8:0:0::7"):
        assert answer(host) == 200, host
    for host in (
        "192.0.2.10",
        "192.0.2.100",
        "::ffff:192.0.2.10",
        "::192.0.2.1",
        "192.0.2.1.example",
        "192.0.2.1 ",
        "0192.0.2.1",
        "2001:db9::7",
        "127.0.0.1",
    ):
        assert answer(host) == 401, host


# ----------------------------------------------------------------------------- no login
LOCAL_ONLY = (421, b"this page is local only")


def test_no_login_is_refused_off_localhost_and_for_a_foreign_host(tmp_path, monkeypatch, base):
    """No login is for this computer alone: refused unless the page's address is on it, and then
    a request whose Host names anything else is answered 421 before anything looks at it, public
    routes included, so another site's script cannot read or post to the page through a name of
    its own that resolves here (whose fetches say same-origin)."""
    for address in (PUBLIC, "http://localhost.example:8110", "https://localhost:8110"):
        s = Settings.from_env({"TRIPBOX_AUTH": "none", "TRIPBOX_BASE_URL": address + base})
        assert [p.key for p in s.problems] == ["TRIPBOX_AUTH"], address
    with pytest.raises(ConfigError, match="TRIPBOX_AUTH=none"):
        client_for(tmp_path / "public", monkeypatch, base=base, env={"TRIPBOX_BASE_URL": PUBLIC})
    c, ctx = client_for(tmp_path, monkeypatch, base=base)
    trip = seed_trip(ctx)
    token = ical.feed_token(ctx.ledger)
    paths = {
        "/": 200,
        f"/trips/{trip.id}": 200,
        "/healthz": 200,
        "/static/app.css": 200,
        f"/feed/{token}.ics": 200,
        "/nothing": 404,
    }
    for host in ("localhost:8110", "127.0.0.1:8110", "[::1]:8110", "localhost", "[::1]"):
        for path, status in paths.items():
            assert raw_get(c.app, f"{base}{path}".encode(), [("host", host)])[0] == status, host
    before = list(ctx.ledger.conn.iterdump())
    foreign = (
        [("host", "evil.example:8110")],
        [("host", "evil.example")],
        [("host", "localhost.example:8110")],
        [("host", "localhost:8110.example")],
        [("host", "127.0.0.1.example")],
        [("host", "[2001:db8::1]:8110")],
        [("host", "")],
        [("host", "localhost:8110"), ("host", "evil.example:8110")],
    )
    same = [("sec-fetch-site", "same-origin")]
    for headers in foreign:
        sent = [*paths, ""] if base else list(paths)
        for path in sent:
            for method in ("GET", "HEAD", "OPTIONS", "POST"):
                status, said, body = raw_get(
                    c.app, f"{base}{path}".encode(), [*headers, *same], method=method
                )
                assert (status, body) == LOCAL_ONLY, (headers, path, method)
                assert said["content-type"].startswith("text/plain"), (headers, path)
                assert "location" not in said and said["cache-control"] == "no-store"
                for name, value in SECURITY_HEADERS.items():
                    assert said[name.lower()] == value, (headers, name)
    # a form post the page's own forms would make, from a name that resolves here: refused
    evil = {"Host": "evil.example:8110", "Sec-Fetch-Site": "same-origin"}
    r = c.post(f"/trips/{trip.id}/notes", data={"notes": "taken"}, headers=evil)
    assert (r.status_code, r.content) == LOCAL_ONLY
    r = c.get(f"/trips/{trip.id}", headers=evil)
    assert (r.status_code, r.content) == LOCAL_ONLY and trip.title not in r.text
    assert list(ctx.ledger.conn.iterdump()) == before
    # the same post under this computer's name is the owner's
    here = {**evil, "Host": "[::1]"}
    r = c.post(
        f"/trips/{trip.id}/notes", data={"notes": "taken"}, headers=here, follow_redirects=False
    )
    assert r.status_code == 303 and ctx.trips.get(trip.id).notes == "taken"
    # a request with no Host names nothing
    assert not auth.names_this_computer(Headers(raw=[]))
