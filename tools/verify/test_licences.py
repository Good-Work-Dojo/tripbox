# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""The dependency licence audit (licences.py): which licences pass, that an expression passes only
when every licence it names does, that a distribution with no readable licence fails by name, that
the generated block of THIRD_PARTY_NOTICES.md is the closure with no version, that --write changes
only the block, and that the project itself is not in the closure. Runs on invented distributions
in temporary trees; nothing is fetched."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import licences as lic  # noqa: E402  (tools/verify is not a package)

MIT = "License :: OSI Approved :: MIT License"


def dist(name, *, expression=None, field=None, classifiers=(), home=None, version="1.0"):
    return {
        "name": name,
        "version": version,
        "license_expression": expression,
        "license": field,
        "classifiers": list(classifiers),
        "home_page": home if home is not None else f"https://example.org/{name}",
    }


GOOD = [
    dist("yamlish", field="MIT"),
    dist("certs", field="MPL-2.0"),
    dist("either", field="MIT OR Apache-2.0"),
    dist("Modern", expression="BSD-3-Clause"),
    dist("tripbox", expression="AGPL-3.0-only"),
]
NOTICES = f"# Notices\n\nby hand\n\n{lic.BEGIN}\n{lic.END}\n\nmore by hand\n"


def tree(tmp_path: Path, text: str = NOTICES) -> Path:
    (tmp_path / lic.NOTICES).write_text(text, encoding="utf-8")
    return tmp_path


def script(tmp_path: Path, dists: list[dict], *args: str):
    f = tmp_path / "dists.json"
    f.write_text(json.dumps(dists), encoding="utf-8")
    return subprocess.run(
        [sys.executable, "-I", str(HERE / "licences.py"), "--dists", str(f), *args, str(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize(
    "licence, ok",
    [
        ("MIT", True),
        ("MIT-0", True),
        ("MIT-CMU", True),
        ("0BSD", True),
        ("CC0-1.0", True),
        ("PSF-2.0", True),
        ("Unlicense", True),
        ("MIT AND (Apache-2.0 OR BSD-3-Clause)", True),
        ("MIT OR GPL-3.0-only", False),
        ("MIT OR GPL-3.0-or-later", False),
        ("LGPL-2.1-or-later", False),
        ("AGPL-3.0-only", False),
        ("SSPL-1.0", False),
        ("BlueOak-1.0.0", False),
        ("Apache-2.0 WITH LLVM-exception", False),
        ("MIT OR", False),
        ("(MIT", False),
        ("mit and apache", False),
    ],
)
def test_an_expression_is_allowed_only_when_every_licence_it_names_is(licence, ok):
    assert lic.allowed(licence) is ok


def test_an_expression_that_names_a_copyleft_licence_fails_by_package(tmp_path):
    root = tree(tmp_path)
    rc, lines = lic.check(root, [dist("dual", expression="MIT OR GPL-3.0-only")])
    assert rc == 1
    assert lines[0] == "licences: dual: MIT OR GPL-3.0-only is not on the allow-list"
    r = script(root, [dist("dual", expression="MIT OR GPL-3.0-only")])
    assert r.returncode == 1 and r.stdout.splitlines()[0] == lines[0]


def test_a_licence_comes_from_its_expression_then_its_field_then_one_versioned_classifier():
    assert lic.licence_of(dist("a", expression="Apache-2.0", field="MIT")) == "Apache-2.0"
    assert lic.licence_of(dist("b", field="MIT", classifiers=(MIT,))) == "MIT"
    assert (
        lic.licence_of(dist("c", field="Permission is hereby granted\nto any", classifiers=(MIT,)))
        == "MIT"
    )
    assert lic.licence_of(dist("d", field="BSD", classifiers=(MIT,))) == "MIT"
    assert lic.licence_of(dist("e", field="GPL")) == "GPL"
    apache = "License :: OSI Approved :: Apache Software License"
    assert lic.licence_of(dist("f", classifiers=(MIT, apache))) is None
    assert lic.licence_of(dist("g", classifiers=(apache,))) is None
    assert lic.licence_of(dist("h")) is None


def test_a_licence_file_read_by_hand_stands_in_for_an_unversioned_classifier():
    bsd = "License :: OSI Approved :: BSD License"
    assert lic.licence_of(dist("Jinja2", classifiers=(bsd,), version="3.1.6")) == "BSD-3-Clause"
    assert lic.licence_of(dist("other", classifiers=(bsd,))) is None
    assert lic.licence_of(dist("Jinja2", expression="MIT")) == "MIT"


def test_a_licence_read_by_hand_holds_for_the_version_read_alone(tmp_path):
    bsd = "License :: OSI Approved :: BSD License"
    newer = dist("Jinja2", classifiers=(bsd,), version="3.2.0")
    assert lic.licence_of(newer) is None
    rc, lines = lic.check(tree(tmp_path), [*GOOD, newer])
    assert rc == 1
    said = "Jinja2: its licence was read by hand for version 3.1.6 only"
    assert any(said in line for line in lines)


def test_one_name_read_twice_keeps_the_reading_the_audit_refuses(tmp_path):
    root = tree(tmp_path)
    twice = [dist("Some_Pkg", field="MIT"), dist("some-pkg", field="GPL-3.0-only")]
    for order in (twice, twice[::-1]):
        packages = lic.closure([*GOOD, *order])
        kept = [p.licence for p in packages if lic._normal(p.name) == "some-pkg"]
        assert kept == ["GPL-3.0-only"]
        rc, lines = lic.check(root, [*GOOD, *order])
        assert rc == 1 and any("GPL-3.0-only is not on the allow-list" in line for line in lines)


def test_an_empty_list_of_packages_fails_whatever_the_notices_hold(tmp_path):
    filled = NOTICES.replace(f"{lic.BEGIN}\n{lic.END}", lic.block(lic.closure(GOOD)))
    for text in (NOTICES, filled):
        rc, lines = lic.check(tree(tmp_path, text), [])
        assert rc == 1, text
        assert lines[0] == "licences: no package was read (run --collect in the runtime image)"


def test_a_distribution_with_no_readable_licence_fails_by_name(tmp_path):
    root = tree(tmp_path)
    rc, lines = lic.check(root, [*GOOD, dist("mystery")])
    assert rc == 1
    assert "licences: mystery: no licence could be read" in lines
    r = script(root, [*GOOD, dist("mystery")])
    assert (
        r.returncode == 1 and "licences: mystery: no licence could be read" in r.stdout.splitlines()
    )


def test_the_generated_block_is_the_closure_and_carries_no_version(tmp_path):
    root = tree(tmp_path)
    r = script(root, GOOD, "--write")
    assert r.returncode == 0, r.stdout
    text = (root / lic.NOTICES).read_text(encoding="utf-8")
    inside = text[text.index(lic.BEGIN) + len(lic.BEGIN) : text.index(lic.END)].strip().splitlines()
    assert inside == [
        "- certs — MPL-2.0 — https://example.org/certs",
        "- either — MIT OR Apache-2.0 — https://example.org/either",
        "- Modern — BSD-3-Clause — https://example.org/Modern",
        "- yamlish — MIT — https://example.org/yamlish",
    ]
    assert "1.0" not in "\n".join(inside)
    r = script(root, GOOD)
    assert r.returncode == 0
    assert r.stdout == "licences: 4 packages, all allowed, the notices are current\n"
    # a version bump leaves the file alone; a package added or dropped does not
    bumped = [{**d, "version": "9.9"} for d in GOOD]
    assert script(root, bumped).returncode == 0
    r = script(root, [*GOOD, dist("extra", field="ISC")])
    assert r.returncode == 1
    assert (
        r.stdout.splitlines()[-1]
        == "licences: THIRD_PARTY_NOTICES.md is not current (run with --write)"
    )
    assert script(root, GOOD[:-2]).returncode == 1


def test_a_home_page_that_is_missing_is_the_projects_page_on_the_index(tmp_path):
    (pkg,) = lic.closure([dist("noway", field="MIT", home="")])
    assert pkg.home == "https://pypi.org/project/noway/"


def test_write_changes_only_the_block(tmp_path):
    root = tree(tmp_path)
    before = (root / lic.NOTICES).read_text(encoding="utf-8")
    assert script(root, GOOD, "--write").returncode == 0
    after = (root / lic.NOTICES).read_text(encoding="utf-8")
    head, tail = before.split(lic.BEGIN)[0], before.split(lic.END)[1]
    assert after.startswith(head) and after.endswith(tail)
    assert after != before
    # a tree with no block, or two, is a finding and is not rewritten
    for text in (
        "no markers\n",
        f"{lic.BEGIN}\n",
        f"{lic.BEGIN}\n{lic.END}\n{lic.BEGIN}\n{lic.END}\n",
    ):
        (root / lic.NOTICES).write_text(text, encoding="utf-8")
        r = script(root, GOOD, "--write")
        assert r.returncode == 1 and "has no block" in r.stdout, text
        assert (root / lic.NOTICES).read_text(encoding="utf-8") == text


def test_the_project_itself_is_not_in_the_closure():
    names = [p.name for p in lic.closure(GOOD)]
    assert "tripbox" not in names
    assert [p.name for p in lic.closure([dist("TripBox", expression="AGPL-3.0-only")])] == []


def test_a_file_that_cannot_be_read_is_exit_2(tmp_path):
    root = tree(tmp_path)
    r = subprocess.run(
        [
            sys.executable,
            "-I",
            str(HERE / "licences.py"),
            "--dists",
            str(root / "none.json"),
            str(root),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode == 2
    (root / "bad.json").write_text("{not json", encoding="utf-8")
    r = subprocess.run(
        [
            sys.executable,
            "-I",
            str(HERE / "licences.py"),
            "--dists",
            str(root / "bad.json"),
            str(root),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode == 2


def test_collect_prints_the_installed_distributions_as_json():
    r = subprocess.run(
        [sys.executable, "-I", str(HERE / "licences.py"), "--collect"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode == 0, r.stderr
    found = json.loads(r.stdout)
    assert found and all(
        {"name", "license_expression", "license", "classifiers", "home_page"} <= d.keys()
        for d in found
    )


def test_the_gate_and_the_workflow_run_the_header_rule_and_the_audit():
    root = HERE.parent.parent
    gate = (root / "tools" / "verify" / "ci.sh").read_text(encoding="utf-8")
    flow = (root / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert gate.index("== licence headers ==") > gate.index("check_workflows.py")
    assert gate.index("python tools/verify/spdx.py") > gate.index("== licence headers ==")
    assert gate.index("== dependency licences ==") > gate.index("== test image ==")
    assert gate.index("== dependency licences ==") < gate.index("== lint ==")
    for text in (gate, flow):
        assert "tripboxoss:rt" in text and "--target runtime" in text
        assert "licences.py --collect" in text and "licences.py --dists" in text
        assert "tools/verify/spdx.py" in text
