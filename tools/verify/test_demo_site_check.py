# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""The check of an exported demo site: a good tree passes, and each fault is named with its file."""

import subprocess
import sys
from pathlib import Path

import pytest

CHECK = Path(__file__).with_name("demo_site_check.py")
BANNER = '<p class="demo-banner" role="note">Demo: invented trips. Read-only.</p>'
ABOUT = (
    '<p class="about muted small">tripbox · <a href="https://source.example/tripbox">Source'
    " code</a></p>"
)


def page(body: str = "", banner: str = BANNER) -> str:
    return f"<!doctype html><html><head></head><body>{banner}{body}{ABOUT}</body></html>"


@pytest.fixture
def site(tmp_path):
    (tmp_path / "trips").mkdir()
    (tmp_path / "static").mkdir()
    (tmp_path / "static" / "app.css").write_text("")
    (tmp_path / ".nojekyll").write_text("")
    (tmp_path / "index.html").write_text(
        page('<link rel="stylesheet" href="static/app.css"><a href="trips/1.html">One</a>'
             '<form><button disabled>Go</button></form>')
    )
    (tmp_path / "trips" / "1.html").write_text(page('<a href="../index.html">Back</a>'))
    return tmp_path


def run(site) -> tuple[int, str]:
    done = subprocess.run([sys.executable, str(CHECK), str(site)], capture_output=True, text=True)
    return done.returncode, done.stdout.strip()


def test_a_good_tree_passes(site):
    assert run(site) == (0, "demo site: ok 2 pages")


@pytest.mark.parametrize(
    "fault, edit",
    [
        ("a script", lambda t: page('<script src="x.js"></script>')),
        ("a control", lambda t: page("<form><button>Go</button></form>")),
        ("an action", lambda t: page('<form action="x"><button disabled>Go</button></form>')),
        ("an absolute address", lambda t: page('<a href="/trips/1">x</a>')),
        ("another host", lambda t: page('<a href="https://elsewhere.example/">x</a>')),
        ("a dangling link", lambda t: page('<a href="missing.html">x</a>')),
        ("no banner", lambda t: page(banner="")),
        ("two banners", lambda t: page(banner=BANNER + BANNER)),
    ],
)
def test_each_fault_is_named_with_its_file(site, fault, edit):
    (site / "trips" / "1.html").write_text(edit(site))
    code, said = run(site)
    assert code == 1 and said.startswith("demo site: FAIL trips/1.html: "), (fault, said)


def test_a_tree_missing_its_marker_or_a_trip_link_fails(site):
    (site / ".nojekyll").unlink()
    assert run(site) == (1, "demo site: FAIL .nojekyll: missing")
    (site / ".nojekyll").write_text("")
    (site / "index.html").write_text(page())
    assert run(site) == (1, "demo site: FAIL index.html: links no trip")
    assert subprocess.run([sys.executable, str(CHECK)], capture_output=True).returncode == 2
