# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
import socket
from pathlib import Path

import pytest

import tripbox

# The end-to-end module's marker, assembled so that this file is not one of those that carry it.
MARKER = "uses_" + "network"


def test_import():
    assert tripbox.__version__


def test_network_guard_refuses_and_reports(no_network):
    with pytest.raises(OSError, match="network access in a test"):
        socket.create_connection(("192.0.2.1", 9), timeout=1)
    with pytest.raises(OSError, match="network access in a test"):
        socket.getaddrinfo("example.com", 443)
    assert len(no_network) == 2
    no_network.clear()


def _try_inet(family, address):
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        with pytest.raises(OSError, match="network access in a test"):
            sock.connect(address)
        with pytest.raises(OSError, match="network access in a test"):
            sock.connect_ex(address)
    finally:
        sock.close()


def test_network_guard_covers_direct_connects_and_spares_unix_sockets(no_network):
    _try_inet(socket.AF_INET, ("192.0.2.1", 9))
    expected = 2
    try:
        socket.socket(socket.AF_INET6).close()
    except OSError:
        pass  # the container has no IPv6 socket support; only the IPv4 half runs
    else:
        _try_inet(socket.AF_INET6, ("2001:db8::1", 9))
        expected = 4
    assert len(no_network) == expected
    no_network.clear()

    a, b = socket.socketpair()
    try:
        a.sendall(b"x")
        assert b.recv(1) == b"x"
    finally:
        a.close()
        b.close()
    assert no_network == []


def test_the_network_guard_refuses_every_name_lookup_and_unconnected_send(no_network):
    """Name lookups the stdlib offers besides getaddrinfo, and a datagram sent with an address
    of its own (no connect), are refused and recorded like a connect."""
    for lookup in (socket.gethostbyname, socket.gethostbyname_ex):
        before = len(no_network)
        with pytest.raises(OSError, match="network access in a test"):
            lookup("example.com")
        assert len(no_network) == before + 1, lookup.__name__
    before = len(no_network)
    # getfqdn swallows the refused lookup and gives the name back: the attempt is still recorded
    assert socket.getfqdn("example.com") == "example.com"
    assert len(no_network) == before + 1
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        before = len(no_network)
        with pytest.raises(OSError, match="network access in a test"):
            udp.sendto(b"x", ("192.0.2.1", 9))
        with pytest.raises(OSError, match="network access in a test"):
            udp.sendto(b"x", 0, ("192.0.2.1", 9))
        with pytest.raises(OSError, match="network access in a test"):
            udp.sendmsg([b"x"], [], 0, ("192.0.2.1", 9))
        assert len(no_network) == before + 3
    finally:
        udp.close()
    assert len(no_network) == 6
    no_network.clear()


def test_network_guard_fails_a_test_that_swallowed_an_attempt(no_network, request):
    from conftest import no_network as fixture

    raw = getattr(fixture, "__wrapped__", None) or fixture._get_wrapped_function()
    with pytest.MonkeyPatch.context() as inner_patches:
        inner = raw(inner_patches, request)
        inner_attempts = next(inner)
        assert inner_attempts is not no_network
        try:
            socket.getaddrinfo("example.com", 443)
        except OSError:
            pass  # swallowed, as the code under test might
        assert inner_attempts and not no_network
        with pytest.raises(AssertionError, match="reached the network"):
            next(inner)
    assert no_network == []


def test_only_the_flightlog_end_to_end_modules_use_the_network():
    tests = Path(__file__).resolve().parent
    carriers = sorted(
        str(path.relative_to(tests)) for path in tests.rglob("*.py") if MARKER in path.read_text()
    )
    assert carriers == ["conftest.py", "test_flightlog_e2e.py", "test_flightlog_matrix.py"]


@getattr(pytest.mark, MARKER)
def test_a_marked_test_outside_the_flightlog_e2e_module_is_still_guarded(no_network):
    # the guard refuses before anything is sent; without it the address is a documentation one
    with pytest.raises(OSError, match="network access in a test"):
        socket.create_connection(("192.0.2.1", 9), timeout=1)
    assert len(no_network) == 1
    no_network.clear()
