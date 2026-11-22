# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""Who is signed in: one `Authenticator` per login TRIPBOX_AUTH names (the page's own password,
`login.PasswordLogin`; a login proxy in front of the page, `ProxyLogin`; Cloudflare Access, whose
token the page verifies itself; or no login at all, on this computer only), which
`middleware.LoginRequired` asks."""

from __future__ import annotations

import ipaddress
import logging
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

import jwt
from jwt import PyJWKClient
from starlette.datastructures import Headers
from starlette.requests import HTTPConnection
from starlette.responses import PlainTextResponse, Response
from starlette.routing import BaseRoute
from starlette.types import ASGIApp, Receive, Scope, Send
from starlette.websockets import WebSocketClose

from tripbox.config import ConfigError, Settings, is_local_origin
from tripbox.web.middleware import security_headers

if TYPE_CHECKING:
    from tripbox.ledger import Ledger

log = logging.getLogger("tripbox.web.auth")

JWKS_LIFESPAN = 3600.0
LEEWAY = 60  # seconds of clock skew tolerated on iat/nbf/exp
# Why a request behind a login proxy is not signed in: one word each, all the log says of it
UNTRUSTED_PEER = "untrusted_peer"
NO_HEADER = "no_header"
TWO_HEADERS = "two_headers"
UNKNOWN_USER = "unknown_user"
# The Host a page with no login answers to: this computer's own names, with any port or none
_LOCAL_HOST = re.compile(r"(?:localhost|127\.0\.0\.1|\[::1\])(?::[0-9]{1,5})?", re.IGNORECASE)
LOCAL_ONLY = "this page is local only"


class AuthError(Exception):
    pass


@dataclass
class AccessJwtVerifier:
    team: str
    audience: str
    allowed_emails: tuple[str, ...]
    jwks: PyJWKClient | None = None

    def __post_init__(self) -> None:
        if not self.team or not self.audience or not self.allowed_emails:
            raise ValueError(
                "TRIPBOX_ACCESS_TEAM, TRIPBOX_ACCESS_AUD, TRIPBOX_ALLOWED_EMAILS needed"
            )
        self.issuer = f"https://{self.team}.cloudflareaccess.com"
        self.jwks = self.jwks or PyJWKClient(
            f"{self.issuer}/cdn-cgi/access/certs",
            cache_jwk_set=True,
            lifespan=JWKS_LIFESPAN,
            timeout=10,
        )
        self._last_fetch_warning = 0.0
        self._last_good: Any = None  # the last key set that verified a token: stale fallback

    def _signing_key(self, token: str):
        """The key for this token's kid: from the JWKS client (cached an hour, refetched on an
        unknown kid), or from the last good key set when the fetch fails — a DNS or Cloudflare
        hiccup must not log the owner out of their own page."""
        try:
            key = self.jwks.get_signing_key_from_jwt(token)  # type: ignore[union-attr]
            self._last_good = self.jwks.get_jwk_set()  # type: ignore[union-attr]
            return key
        except jwt.PyJWKClientConnectionError as exc:
            now = time.monotonic()
            if now - self._last_fetch_warning > 3600:  # once an hour, not once a request
                self._last_fetch_warning = now
                log.warning("Access JWKS fetch failed: %s", exc)
            if self._last_good is None:
                raise AuthError("certs unavailable") from exc
            kid = jwt.get_unverified_header(token).get("kid")
            for k in self._last_good.keys:
                if k.key_id == kid:
                    return k
            raise AuthError("certs unavailable and the token's key is not a known one") from exc

    def verify(self, token: str) -> dict[str, Any]:
        """Claims of a valid Access user token for this app, else AuthError."""
        try:
            key = self._signing_key(token)
            claims = jwt.decode(
                token,
                key.key,
                algorithms=["RS256"],
                audience=self.audience,
                issuer=self.issuer,
                leeway=LEEWAY,
                options={"require": ["exp", "iat", "aud", "iss"]},
            )
        except AuthError:
            raise
        except (jwt.PyJWTError, ValueError) as exc:
            raise AuthError(str(exc)[:120]) from exc
        email = str(claims.get("email") or "").lower()
        if email not in self.allowed_emails:
            raise AuthError("email not allowed")
        return claims


class Authenticator(Protocol):
    """How the page knows who is signed in. `name` is the TRIPBOX_AUTH value it serves."""

    name: str

    def authenticate(self, request: HTTPConnection) -> str | None:
        """Who is signed in on this request, or None."""
        ...

    def challenge(self, request: HTTPConnection) -> Response:
        """What a request with nobody signed in gets."""
        ...

    def routes(self) -> list[BaseRoute]:
        """The login's own routes, each one public (by name, `app.PUBLIC`)."""
        ...

    def logout_url(self) -> str | None:
        """Where signing out happens, or None when there is nothing to sign out of."""
        ...


def names_this_computer(headers: Headers) -> bool:
    """Whether a request's Host names this computer: one Host, `localhost`, `127.0.0.1` or
    `[::1]`, with any port or none. None, two, or any other name does not."""
    hosts = headers.getlist("host")
    return len(hosts) == 1 and _LOCAL_HOST.fullmatch(hosts[0]) is not None


def local_only() -> Response:
    """What a page with no login answers a request that does not name this computer: 421, with
    the security headers (it may be sent before the wrapper that sets them)."""
    return PlainTextResponse(LOCAL_ONLY, status_code=421, headers=security_headers(""))


class NoLogin:
    """No sign-in, for a page on this computer only (`build` takes it only with a local base
    URL): a request whose Host names this computer (`names_this_computer`) is its owner's,
    "local"; any other is answered 421, so another site's script cannot read the page through a
    name of its own that resolves here. `create_app` wraps the whole page in `LocalOnly`, so the
    rule holds before anything else, public routes included."""

    name = "none"

    def authenticate(self, request: HTTPConnection) -> str | None:
        return "local" if names_this_computer(request.headers) else None

    def challenge(self, request: HTTPConnection) -> Response:
        return local_only()

    def routes(self) -> list[BaseRoute]:
        return []

    def logout_url(self) -> str | None:
        return None


class LocalOnly:
    """The page with no login, wrapped: a request (http or websocket) whose Host does not name
    this computer is answered `local_only` (a websocket is closed) before anything else looks at
    it. `state` is the page's."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.state = getattr(app, "state", None)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in ("http", "websocket") and not names_this_computer(Headers(scope=scope)):
            if scope["type"] == "http":
                await local_only()(scope, receive, send)
            else:
                await WebSocketClose(code=1008)(scope, receive, send)
            return
        await self.app(scope, receive, send)


Address = ipaddress.IPv4Address | ipaddress.IPv6Address


def peer_address(scope: Mapping[str, Any]) -> Address | None:
    """The request's socket peer (`scope["client"]`, which no forwarded header changes: the page
    is served with uvicorn's proxy headers off) read as an address, an IPv4-mapped IPv6 one as
    the IPv4 address it maps; None when the scope has no peer or its host is no address (a Unix
    socket, a test client's name)."""
    client = scope.get("client")
    host = client[0] if isinstance(client, (tuple, list)) and client else None
    if not isinstance(host, str):
        return None
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return None
    if address.version == 6 and address.ipv4_mapped is not None:
        return address.ipv4_mapped
    return address


class ProxyLogin(Authenticator):
    """A login proxy in front of the page (Authelia, Authentik, oauth2-proxy and the like) signs
    people in and names the user in a header (TRIPBOX_PROXY_USER_HEADER). Anyone can send that
    header, so it is believed only on a connection from the proxy itself: the socket peer
    (`peer_address`) must be in TRIPBOX_TRUSTED_PROXIES, or the header is ignored whatever it
    says. It must then occur exactly once, and its value, trimmed of spaces and tabs, be one of
    TRIPBOX_PROXY_USERS, case aside. Any other request is a 401, and one INFO line gives the
    reason as one word (UNTRUSTED_PEER, NO_HEADER, TWO_HEADERS, UNKNOWN_USER), never the
    header's value or the peer."""

    name = "proxy"

    def __init__(self, settings: Settings) -> None:
        problems = [str(p) for p in settings.stopping("web")]
        if settings.tripbox_auth != "proxy" or problems:
            raise ConfigError("\n".join(problems) or "TRIPBOX_AUTH is not proxy")
        networks, users = settings.trusted_proxies, settings.proxy_users
        if not networks or not users or any(n.prefixlen == 0 for n in networks):
            raise ConfigError(
                "TRIPBOX_TRUSTED_PROXIES must list the proxy's addresses, not every address,"
                " and TRIPBOX_PROXY_USERS its users"
            )
        self.networks = networks
        self.header = settings.proxy_user_header
        self.users = frozenset(user.lower() for user in users)

    def authenticate(self, request: HTTPConnection) -> str | None:
        user, refused = self._user(request)
        if refused:
            log.info("proxy sign-in refused: %s", refused)
        return user

    def _user(self, request: HTTPConnection) -> tuple[str | None, str]:
        """The user the proxy names, or None and why not."""
        peer = peer_address(request.scope)
        if peer is None or not any(peer in network for network in self.networks):
            return None, UNTRUSTED_PEER
        values = request.headers.getlist(self.header)
        if not values:
            return None, NO_HEADER
        if len(values) > 1:
            return None, TWO_HEADERS
        name = values[0].strip(" \t")
        if not name.isascii() or name.lower() not in self.users:
            return None, UNKNOWN_USER
        return name.lower(), ""

    def challenge(self, request: HTTPConnection) -> Response:
        return PlainTextResponse("unauthorized", status_code=401)

    def routes(self) -> list[BaseRoute]:
        return []

    def logout_url(self) -> str | None:
        return None  # signing out happens at the proxy


class CloudflareAccess:
    """Cloudflare Access in front of the page, its token verified here too: a request is signed
    in when its Cf-Access-Jwt-Assertion is a valid token for this app from an allowed address."""

    name = "cloudflare"

    def __init__(self, verifier: AccessJwtVerifier) -> None:
        self.verifier = verifier

    def authenticate(self, request: HTTPConnection) -> str | None:
        token = request.headers.get("cf-access-jwt-assertion")
        if not token:
            return None
        try:
            claims = self.verifier.verify(token)
        except AuthError as exc:
            log.info("rejected request to %s: %s", request.scope.get("path", ""), exc)
            return None
        return str(claims["email"])

    def challenge(self, request: HTTPConnection) -> Response:
        return PlainTextResponse("unauthorized", status_code=401)

    def routes(self) -> list[BaseRoute]:
        return []

    def logout_url(self) -> str | None:
        return f"{self.verifier.issuer}/cdn-cgi/access/logout"


def build(
    settings: Settings, ledger_factory: Callable[[], Ledger], *, owns_ledger: bool = True
) -> Authenticator:
    """The login TRIPBOX_AUTH names, for the page over the ledger `ledger_factory` opens (and,
    when `owns_ledger`, closes again)."""
    if settings.tripbox_auth == "password":
        from tripbox.web.login import PasswordLogin  # it builds on this module's interface

        return PasswordLogin(settings, ledger_factory, owns_ledger=owns_ledger)
    if settings.tripbox_auth == "proxy":
        return ProxyLogin(settings)
    if settings.tripbox_auth == "cloudflare":
        return CloudflareAccess(
            AccessJwtVerifier(
                settings.tripbox_access_team,
                settings.tripbox_access_aud,
                settings.tripbox_allowed_emails,
            )
        )
    if settings.tripbox_auth == "none":
        if not is_local_origin(settings.origin):
            raise ConfigError("TRIPBOX_AUTH=none serves the page with no login: on localhost only")
        return NoLogin()
    raise ConfigError(
        f"TRIPBOX_AUTH must be password, proxy, cloudflare or none, not {settings.tripbox_auth!r}"
    )
