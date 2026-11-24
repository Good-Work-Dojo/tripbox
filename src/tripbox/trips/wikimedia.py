# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""Find a freely licensed photo of a place on Wikipedia and Wikimedia Commons (httpx2).

The page image of the place's English Wikipedia article is looked up on Commons; it is kept only
when its licence is free (CC0, public domain, CC BY, CC BY-SA), then downloaded with a size cap
from Wikimedia's upload host alone (`UPLOAD_HOST`, over https).
`None` means nothing usable exists; `Transient` means ask again later. Only the worker imports
this module: the web container makes no outbound request."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from html.parser import HTMLParser

import httpx2

from tripbox import __version__

PROJECT_URL = "https://github.com/Good-Work-Dojo/tripbox"
TIMEOUT = 10.0
MAX_BYTES = 10 * 1024 * 1024
ARTIST_MAX = 120
WIKIPEDIA_API = "https://en.wikipedia.org/w/api.php"
COMMONS_API = "https://commons.wikimedia.org/w/api.php"
FREE_LICENCE = re.compile(r"^(cc0|pd|pd-.*|cc-by-[0-9.]+|cc-by-sa-[0-9.]+)$")
NOT_FOUND = (404, 410)  # the one refusal that settles a lookup as none
# The one host an image is downloaded from, over https; a redirect is followed only on it
UPLOAD_HOST = "upload.wikimedia.org"
MAX_REDIRECTS = 3


class Transient(Exception):
    """A failure worth retrying later; `code` is short: timeout, http_429, http_4xx, http_5xx,
    bad_json, network."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Found:
    data: bytes
    credit: dict[str, str]  # artist, licence, licence_url, source_url


def client(source_url: str = "", *, transport: httpx2.BaseTransport | None = None) -> httpx2.Client:
    """A client naming tripbox and its source, as Wikimedia asks, with a 10 s timeout."""
    url = source_url.strip() or PROJECT_URL
    return httpx2.Client(
        timeout=TIMEOUT,
        transport=transport,
        follow_redirects=True,
        headers={"User-Agent": f"tripbox/{__version__} (+{url})"},
    )


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def _plain(html: str) -> str:
    parser = _Text()
    parser.feed(html)
    parser.close()
    return " ".join("".join(parser.parts).split())


def _check(r: httpx2.Response) -> None:
    """Only a 404 or a 410 says there is nothing (answered as none by the caller); any other
    refusal (a 403 for the User-Agent, a 429, a 5xx) is asked again later, so one bad day never
    keeps every trip without a photo for NONE_DAYS."""
    if r.status_code == 429:
        raise Transient("http_429")
    if r.status_code >= 500:
        raise Transient("http_5xx")
    if r.status_code >= 400 and r.status_code not in NOT_FOUND:
        raise Transient("http_4xx")


def _api(http: httpx2.Client, url: str, params: dict[str, str]) -> dict:
    try:
        r = http.get(url, params=params)
    except httpx2.TimeoutException as exc:
        raise Transient("timeout") from exc
    except httpx2.HTTPError as exc:
        raise Transient("network") from exc
    _check(r)
    if r.status_code >= 400:
        return {}
    try:
        body = json.loads(r.content)
    except ValueError as exc:
        raise Transient("bad_json") from exc
    return body if isinstance(body, dict) else {}


def _first_page(body: dict) -> dict:
    pages = (body.get("query") or {}).get("pages")
    if isinstance(pages, list) and pages and isinstance(pages[0], dict):
        return pages[0]
    return {}


def _value(meta: dict, key: str) -> str:
    entry = meta.get(key)
    return str(entry.get("value", "")) if isinstance(entry, dict) else ""


def _on_upload_host(url: httpx2.URL) -> bool:
    """An https address on Wikimedia's upload host, its own port, with no login in it."""
    return (
        url.scheme == "https"
        and url.host == UPLOAD_HOST
        and url.port in (None, 443)
        and not url.userinfo
    )


def _download(http: httpx2.Client, url: str) -> bytes | None:
    """The image at `url`, at most MAX_BYTES, fetched only from UPLOAD_HOST: an address
    elsewhere, or a redirect off it (or past MAX_REDIRECTS), is no image."""
    try:
        at = httpx2.URL(url)
    except (httpx2.InvalidURL, TypeError, ValueError):
        return None
    data = bytearray()
    try:
        for _ in range(MAX_REDIRECTS + 1):
            if not _on_upload_host(at):
                return None
            with http.stream("GET", at, follow_redirects=False) as r:
                if r.has_redirect_location:
                    at = r.url.join(r.headers["location"])
                    continue
                _check(r)
                if r.status_code >= 300:
                    return None
                for chunk in r.iter_bytes():
                    data += chunk
                    if len(data) > MAX_BYTES:
                        return None
                return bytes(data) or None
    except httpx2.TimeoutException as exc:
        raise Transient("timeout") from exc
    except httpx2.HTTPError as exc:
        raise Transient("network") from exc
    return None


def find(place: str, *, http: httpx2.Client) -> Found | None:
    page = _first_page(
        _api(
            http,
            WIKIPEDIA_API,
            {
                "action": "query",
                "format": "json",
                "formatversion": "2",
                "titles": place,
                "redirects": "1",
                "prop": "pageimages|pageprops",
                "piprop": "name",
            },
        )
    )
    name = page.get("pageimage")
    if page.get("missing") or not isinstance(name, str) or not name:
        return None
    if "disambiguation" in (page.get("pageprops") or {}):
        return None

    file = _first_page(
        _api(
            http,
            COMMONS_API,
            {
                "action": "query",
                "format": "json",
                "formatversion": "2",
                "titles": f"File:{name}",
                "prop": "imageinfo",
                "iiprop": "url|extmetadata",
                "iiurlwidth": "1600",
            },
        )
    )
    infos = file.get("imageinfo")
    if file.get("missing") or not isinstance(infos, list) or not infos:
        return None
    info = infos[0] if isinstance(infos[0], dict) else {}
    meta = info.get("extmetadata") if isinstance(info.get("extmetadata"), dict) else {}
    licence = _value(meta, "License").strip().casefold()
    if not FREE_LICENCE.match(licence) or _value(meta, "NonFree").strip().casefold() == "true":
        return None
    url = info.get("thumburl")
    if not isinstance(url, str) or not url:
        return None

    artist = _plain(_value(meta, "Artist"))[:ARTIST_MAX].strip() or "Unknown author"
    credit = {
        "artist": artist,
        "licence": _value(meta, "LicenseShortName"),
        "licence_url": _value(meta, "LicenseUrl"),
        "source_url": str(info.get("descriptionurl") or ""),
    }
    data = _download(http, url)
    return Found(data, credit) if data else None
