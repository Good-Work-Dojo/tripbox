# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
import httpx2
import pytest

from tripbox import __version__
from tripbox.trips import wikimedia
from tripbox.trips.wikimedia import Found, Transient, client, find

IMAGE = b"\xff\xd8sample-bytes"


def meta(**over):
    base = {
        "License": {"value": "cc-by-sa-4.0"},
        "LicenseShortName": {"value": "CC BY-SA 4.0"},
        "LicenseUrl": {"value": "https://example.org/licence"},
        "Artist": {"value": "Sample Author"},
        "NonFree": {"value": "false"},
    }
    base.update(over)
    return base


def build(*, wiki=None, commons=None, image=IMAGE, seen=None, status=None):
    if wiki is None:
        wiki = {"query": {"pages": [{"title": "X", "pageimage": "X.jpg"}]}}
    commons = (
        commons
        if commons is not None
        else {
            "query": {
                "pages": [
                    {
                        "imageinfo": [
                            {
                                "thumburl": "https://upload.wikimedia.org/x.jpg",
                                "descriptionurl": "https://commons.example.org/File:X.jpg",
                                "extmetadata": meta(),
                            }
                        ]
                    }
                ]
            }
        }
    )

    def handler(request):
        if seen is not None:
            seen.append(request)
        host = request.url.host
        if status and host in status:
            return httpx2.Response(status[host], content=b"x")
        if host == "en.wikipedia.org":
            return wiki if isinstance(wiki, httpx2.Response) else httpx2.Response(200, json=wiki)
        if host == "commons.wikimedia.org":
            return httpx2.Response(200, json=commons)
        return httpx2.Response(200, content=image)

    return client("", transport=httpx2.MockTransport(handler))


def commons_with(**over):
    return {
        "query": {
            "pages": [
                {
                    "imageinfo": [
                        {
                            "thumburl": "https://upload.wikimedia.org/x.jpg",
                            "descriptionurl": "https://commons.example.org/File:X.jpg",
                            "extmetadata": meta(**over),
                        }
                    ]
                }
            ]
        }
    }


def test_free_photo_is_found_with_its_credit():
    seen = []
    got = find("Sampleton", http=build(seen=seen))
    assert got == Found(
        IMAGE,
        {
            "artist": "Sample Author",
            "licence": "CC BY-SA 4.0",
            "licence_url": "https://example.org/licence",
            "source_url": "https://commons.example.org/File:X.jpg",
        },
    )
    first, second = seen[0].url.params, seen[1].url.params
    assert first["titles"] == "Sampleton" and first["redirects"] == "1"
    assert first["prop"] == "pageimages|pageprops" and first["piprop"] == "name"
    assert second["titles"] == "File:X.jpg" and second["iiurlwidth"] == "1600"
    assert second["iiprop"] == "url|extmetadata"


def test_user_agent_names_tripbox_and_its_source():
    seen = []
    find("Sampleton", http=build(seen=seen))
    assert {r.headers["user-agent"] for r in seen} == {
        f"tripbox/{__version__} (+{wikimedia.PROJECT_URL})"
    }
    c = client("https://example.org/src")
    assert c.headers["user-agent"] == f"tripbox/{__version__} (+https://example.org/src)"
    assert c.timeout.read == 10.0 and c.timeout.connect == 10.0


def test_non_free_file_is_none():
    assert find("S", http=build(commons=commons_with(NonFree={"value": "true"}))) is None


def test_non_free_licence_is_none():
    assert find("S", http=build(commons=commons_with(License={"value": "fair-use"}))) is None


@pytest.mark.parametrize("lic", ["CC0", "pd", "PD-old-70", "cc-by-4.0", "cc-by-sa-3.0"])
def test_free_licences_pass(lic):
    assert find("S", http=build(commons=commons_with(License={"value": lic}))) is not None


def test_file_missing_on_commons_is_none():
    commons = {"query": {"pages": [{"title": "File:X.jpg", "missing": True}]}}
    assert find("S", http=build(commons=commons)) is None


def test_disambiguation_is_none():
    wiki = {"query": {"pages": [{"pageimage": "X.jpg", "pageprops": {"disambiguation": ""}}]}}
    assert find("S", http=build(wiki=wiki)) is None


def test_no_page_or_no_image_is_none():
    gone = {"query": {"pages": [{"title": "S", "missing": True}]}}
    bare = {"query": {"pages": [{"title": "S"}]}}
    assert find("S", http=build(wiki=gone)) is None
    assert find("S", http=build(wiki=bare)) is None


@pytest.mark.parametrize(("status", "code"), [(429, "http_429"), (503, "http_5xx")])
@pytest.mark.parametrize(
    "host", ["en.wikipedia.org", "commons.wikimedia.org", "upload.wikimedia.org"]
)
def test_throttle_and_server_errors_are_transient(status, code, host):
    with pytest.raises(Transient) as e:
        find("S", http=build(status={host: status}))
    assert e.value.code == code


@pytest.mark.parametrize("status", [400, 401, 403, 405, 418, 451])
@pytest.mark.parametrize(
    "host", ["en.wikipedia.org", "commons.wikimedia.org", "upload.wikimedia.org"]
)
def test_a_refusal_other_than_not_found_is_transient(status, host):
    """A 403 (Wikimedia refusing the User-Agent) or any other 4xx says nothing of the place, so
    it is asked again later, never kept as nothing found for 30 days."""
    with pytest.raises(Transient) as e:
        find("S", http=build(status={host: status}))
    assert e.value.code == "http_4xx"


@pytest.mark.parametrize("status", [404, 410])
@pytest.mark.parametrize(
    "host", ["en.wikipedia.org", "commons.wikimedia.org", "upload.wikimedia.org"]
)
def test_not_found_or_gone_is_none(status, host):
    assert find("S", http=build(status={host: status})) is None


def test_non_json_is_transient():
    with pytest.raises(Transient) as e:
        find("S", http=build(wiki=httpx2.Response(200, content=b"<html>")))
    assert e.value.code == "bad_json"


def test_timeout_and_network_errors_are_transient():
    def boom(exc):
        def handler(request):
            raise exc

        return client("", transport=httpx2.MockTransport(handler))

    with pytest.raises(Transient) as e:
        find("S", http=boom(httpx2.ReadTimeout("slow")))
    assert e.value.code == "timeout"
    with pytest.raises(Transient) as e:
        find("S", http=boom(httpx2.ConnectError("down")))
    assert e.value.code == "network"


def test_oversize_image_is_none_and_not_buffered_past_the_cap():
    pulled = []

    def chunks():
        for _ in range(40):
            pulled.append(1)
            yield b"a" * (1024 * 1024)

    assert find("S", http=build(image=chunks())) is None
    assert len(pulled) <= 11


def test_image_exactly_at_the_cap_is_kept():
    data = b"a" * wikimedia.MAX_BYTES
    assert find("S", http=build(image=data)).data == data


def test_artist_html_is_stripped_and_capped():
    html = '<a href="https://example.org/u">Sample  &amp; Co</a>\n <span>Photo</span>'
    got = find("S", http=build(commons=commons_with(Artist={"value": html})))
    assert got.credit["artist"] == "Sample & Co Photo"
    long = find("S", http=build(commons=commons_with(Artist={"value": "<b>" + "a" * 300 + "</b>"})))
    assert len(long.credit["artist"]) == 120


def test_missing_artist_is_unknown_and_pd_may_lack_a_licence_url():
    commons = commons_with(Artist={"value": " <i></i> "}, LicenseUrl={"value": ""})
    got = find("S", http=build(commons=commons))
    assert got.credit["artist"] == "Unknown author" and got.credit["licence_url"] == ""
