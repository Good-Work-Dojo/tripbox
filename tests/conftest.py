# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
import os
import socket
import sys
import time
from pathlib import Path

import pytest
from helpers.web import BASES

# The modules whose tests may use the network: they run only inside the throwaway AirTrail stack.
E2E_MODULES = tuple(
    Path(__file__).resolve().parent / name
    for name in ("test_flightlog_e2e.py", "test_flightlog_matrix.py")
)


@pytest.fixture
def tmp_ledger_path(tmp_path):
    return tmp_path / "ledger.db"


@pytest.fixture(params=BASES, ids=lambda b: b or "/")
def base(request):
    """The base path the page of a page test is served under: each of `helpers.web.BASES`."""
    return request.param


@pytest.fixture(autouse=True)
def fresh_lists():
    """No list read (shipped or from a config directory) outlives its test."""
    from tripbox import lists

    lists.clear_caches()
    yield
    lists.clear_caches()


@pytest.fixture(autouse=True)
def no_unscripted_model_request():
    """A request a fake model server had nothing scripted for fails its test here, at teardown,
    even when the provider turned the failure the SDK made of it into an answer of its own."""
    yield
    fake = sys.modules.get("helpers.modelserver")
    if fake is not None:
        stray, fake.UNSCRIPTED[:] = list(fake.UNSCRIPTED), []
        assert not stray, f"a fake model server was sent what nothing scripted: {stray}"


@pytest.fixture(autouse=True)
def keep_process_zone():
    """A command makes the configured zone the process's own (`util.use_zone`): every test ends
    with the zone, and the TZ variable, the process had before it."""
    saved = os.environ.get("TZ")
    yield
    if saved is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = saved
    time.tzset()


@pytest.fixture(autouse=True)
def no_network(monkeypatch, request):
    """Refuse every name lookup (getaddrinfo, gethostbyname, gethostbyname_ex, gethostbyaddr,
    which getfqdn uses, and getnameinfo), every non-UNIX connect and every non-UNIX send to an
    address (sendto, sendmsg), and fail the test that tried one.

    The attempts are recorded and checked at teardown, so a caller that swallows the
    error still fails its test. A test that provokes an attempt on purpose clears the
    returned list before it ends. A test marked `uses_network` in one of the end-to-end
    modules (whose tests talk to the throwaway AirTrail) is left alone: nothing is patched.
    The marker anywhere else exempts nothing.
    """
    attempts: list = []
    marked = request.node.get_closest_marker("uses_network") is not None
    if marked and Path(request.node.path).resolve() in E2E_MODULES:
        yield attempts
        return
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_sendto = socket.socket.sendto
    real_sendmsg = socket.socket.sendmsg

    def refuse(destination):
        attempts.append(destination)
        raise OSError("network access in a test")

    def getaddrinfo(host, port, *args, **kwargs):
        refuse((host, port))

    def lookup(name, *args, **kwargs):
        refuse(name)

    def sendto(self, data, *args):
        # sendto(data, address) or sendto(data, flags, address)
        if self.family == socket.AF_UNIX:
            return real_sendto(self, data, *args)
        refuse(args[-1] if args else None)

    def sendmsg(self, buffers, *args):
        if self.family == socket.AF_UNIX:
            return real_sendmsg(self, buffers, *args)
        refuse(args[2] if len(args) > 2 else None)

    def connect(self, address):
        if self.family == socket.AF_UNIX:
            return real_connect(self, address)
        refuse(address)

    def connect_ex(self, address):
        if self.family == socket.AF_UNIX:
            return real_connect_ex(self, address)
        refuse(address)  # raises where the stdlib returns an errno, on purpose: nobody misses it

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    for name in ("gethostbyname", "gethostbyname_ex", "gethostbyaddr", "getnameinfo"):
        monkeypatch.setattr(socket, name, lookup)
    monkeypatch.setattr(socket.socket, "sendto", sendto)
    monkeypatch.setattr(socket.socket, "sendmsg", sendmsg)
    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    yield attempts
    assert not attempts, f"a test reached the network: {len(attempts)} attempt(s): {attempts!r}"
