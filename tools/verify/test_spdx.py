# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""The licence header (tools/verify/spdx.py): two comment lines in each source file's own comment
syntax, after what must stay first (a shebang, a Python coding line, a Dockerfile's parser
directives), never changing what a file does, and none on a data file, a private tool or the one
file that is another licence's. Reads the repository; writes only temporary copies."""

import ast
import re
import shutil
import subprocess
import sys
from pathlib import Path

import jinja2
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "verify"))
import spdx  # noqa: E402  (tools/verify is not a package)

LICENSE_LINE = "SPDX-License-Identifier: AGPL-3.0-only"
TERMS_LINE = "Additional terms: see NOTICE"
PATCH = "integrations/airtrail/trips-dock.patch"


def put(root: Path, rel: str, text: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(text.encode("utf-8"))
    return p


def run(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-I", str(ROOT / "tools" / "verify" / "spdx.py"), *args, str(root)],
        capture_output=True,
        text=True,
        check=False,
    )


def test_every_source_of_the_tree_carries_the_header():
    assert spdx.missing(ROOT) == [], "run tools/verify/spdx.py --write"
    r = run(ROOT)
    assert r.returncode == 0, r.stdout
    assert re.fullmatch(r"spdx: \d+ sources, all carry the header\n", r.stdout), r.stdout


STYLES = {
    "a.py": ('"""Doc."""\nx = 1\n', ["# " + LICENSE_LINE, "# " + TERMS_LINE]),
    "a.sh": ("echo hi\n", ["# " + LICENSE_LINE, "# " + TERMS_LINE]),
    "a.yaml": ("key: 1\n", ["# " + LICENSE_LINE, "# " + TERMS_LINE]),
    "a.yml": ("key: 1\n", ["# " + LICENSE_LINE, "# " + TERMS_LINE]),
    "a.js": ("var x = 1;\n", ["// " + LICENSE_LINE, "// " + TERMS_LINE]),
    "a.css": ("body { }\n", ["/* " + LICENSE_LINE + " */", "/* " + TERMS_LINE + " */"]),
    "a.html": ("<p>x</p>\n", ["{# " + LICENSE_LINE + " -#}", "{# " + TERMS_LINE + " -#}"]),
    "Dockerfile": ("FROM scratch\n", ["# " + LICENSE_LINE, "# " + TERMS_LINE]),
}


@pytest.mark.parametrize("shebang", [False, True])
@pytest.mark.parametrize("name", sorted(STYLES))
def test_write_adds_the_two_lines_and_changes_no_other_byte(tmp_path, name, shebang):
    body, header = STYLES[name]
    if shebang and name.endswith((".py", ".sh")):
        body = "#!/usr/bin/env python3\n" + body
    elif shebang:
        pytest.skip("a shebang belongs to a script")
    if name == "a.py" and shebang:
        body = body.replace("\n", "\n# -*- coding: utf-8 -*-\n", 1)
    put(tmp_path, f"src/{name}", body)
    original = body.encode()
    assert run(tmp_path, "--write").returncode == 0
    after = (tmp_path / "src" / name).read_bytes()
    lines = after.decode().splitlines(keepends=True)
    kept = 0
    while kept < len(lines) and (
        lines[kept].startswith("#!") or "coding:" in lines[kept]
    ):
        kept += 1
    assert [x.rstrip("\n") for x in lines[kept : kept + 2]] == header
    # removing the two lines gives back every original byte
    assert "".join(lines[:kept] + lines[kept + 2 :]).encode() == original
    # a second write changes nothing
    run(tmp_path, "--write")
    assert (tmp_path / "src" / name).read_bytes() == after


def test_a_python_docstring_stays_the_first_statement_and_parser_directives_stay_first(tmp_path):
    put(tmp_path, "src/m.py", '"""Doc."""\nx = 1\n')
    put(tmp_path, "Dockerfile", "# syntax=docker/dockerfile:1\n# escape=`\nFROM scratch\n")
    run(tmp_path, "--write")
    tree = ast.parse((tmp_path / "src" / "m.py").read_text())
    assert ast.get_docstring(tree) == "Doc."
    docker = (tmp_path / "Dockerfile").read_text().splitlines()
    assert docker[:2] == ["# syntax=docker/dockerfile:1", "# escape=`"]
    assert docker[2:4] == ["# " + LICENSE_LINE, "# " + TERMS_LINE]


def test_a_missing_header_is_named_and_fails_the_run(tmp_path):
    put(tmp_path, "src/a.py", "x = 1\n")
    r = run(tmp_path)
    assert r.returncode == 1
    assert r.stdout.splitlines()[0] == "spdx: src/a.py lacks its header"


def test_a_data_file_and_a_private_tool_are_not_sources(tmp_path):
    for rel in (
        "tests/corpus/01/answer.json",
        "tests/corpus/config/travellers.yaml",
        "tests/data/x/f.json",
        "tests/data/x/s.yaml",
        "src/x/data.json",
        "src/x/notes.md",
        "src/x/notes.txt",
        "src/x/pyproject.toml",
        "src/x/f.woff2",
        "src/x/i.png",
        "src/x/c.ics",
        "src/x/.hidden.py",
        "src/x/e.eml",
        "docs/proxy/nginx-hostname.conf",
        "docs/proxy/traefik-hostname.yml",
        "tools/private/dk.sh",
        "tools/private/x.py",
        "uv.lock",
        "pyproject.toml",
        "config/travellers.example.yaml",
    ):
        put(tmp_path, rel, "x\n")
    assert spdx.sources(tmp_path) == []
    put(tmp_path, "scripts/s.py", "x = 1\n")
    put(tmp_path, "integrations/airtrail/build.sh", "true\n")
    put(tmp_path, ".github/workflows/ci.yml", "name: t\n")
    put(tmp_path, "tools/verify/t.py", "x = 1\n")
    put(tmp_path, "compose.yaml", "services: {}\n")
    put(tmp_path, "Dockerfile", "FROM scratch\n")
    found = {p.relative_to(tmp_path).as_posix() for p in spdx.sources(tmp_path)}
    assert found == {
        "scripts/s.py",
        "integrations/airtrail/build.sh",
        ".github/workflows/ci.yml",
        "tools/verify/t.py",
        "compose.yaml",
        "Dockerfile",
    }
    assert run(tmp_path, "--write").returncode == 0
    assert (tmp_path / "docs/proxy/nginx-hostname.conf").read_text() == "x\n"
    assert (tmp_path / "tools/private/x.py").read_text() == "x\n"
    assert (tmp_path / "tests/corpus/config/travellers.yaml").read_text() == "x\n"


def test_the_patch_is_gpl_and_is_never_rewritten(tmp_path):
    assert spdx.OTHER == {PATCH: "GPL-3.0-only"}
    assert (ROOT / "LICENSES" / "GPL-3.0-only.txt").is_file()
    head = (ROOT / PATCH).read_text().split("diff --git", 1)[0]
    assert "SPDX-License-Identifier: GPL-3.0-only" in head
    # a copy that lacks the line, or lacks the GPL text, is named; --write leaves it as it is
    bare = "Some words.\n\ndiff --git a/x b/x\n--- a/x\n+++ b/x\n"
    put(tmp_path, PATCH, bare)
    (tmp_path / "LICENSES").mkdir()
    r = run(tmp_path)
    assert r.returncode == 1
    assert f"spdx: {PATCH} lacks its header" in r.stdout
    assert "LICENSES/GPL-3.0-only.txt" in r.stdout
    run(tmp_path, "--write")
    assert (tmp_path / PATCH).read_text() == bare
    # the identifier below the first diff line does not count
    put(tmp_path, PATCH, bare + "+SPDX-License-Identifier: GPL-3.0-only\n")
    assert f"spdx: {PATCH} lacks its header" in run(tmp_path).stdout
    # a patch that holds the identifier above its diff and the text beside it is clean
    shutil.copy(ROOT / PATCH, tmp_path / PATCH)
    (tmp_path / "LICENSES" / "GPL-3.0-only.txt").write_text("text\n")
    r = run(tmp_path)
    assert r.returncode == 0, r.stdout
    assert r.stdout == "spdx: 1 sources, all carry the header\n"


def test_a_templates_two_lines_render_nothing():
    env = jinja2.Environment(autoescape=True)
    templates = sorted((ROOT / "src" / "tripbox" / "web" / "templates").glob("*.html"))
    assert len(templates) > 10
    lines = [spdx.HTML.open + x + spdx.HTML.close for x in (spdx.LICENSE_ID, spdx.TERMS)]
    assert lines == ["{# " + LICENSE_LINE + " -#}", "{# " + TERMS_LINE + " -#}"]
    assert env.from_string("\n".join(lines)).render() == ""
    for t in templates:
        text = t.read_text(encoding="utf-8")
        assert text.splitlines()[:2] == lines, t.name
        assert env.from_string("\n".join(text.splitlines()[:2])).render() == "", t.name


class _Anything(jinja2.ChainableUndefined):
    """A name, attribute or call the render has no value for: nothing, and never an error."""

    def __call__(self, *args, **kwargs):
        return self


def render_all(directory: Path) -> dict[str, tuple]:
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(directory), autoescape=True, undefined=_Anything
    )
    out = {}
    for t in sorted(directory.glob("*.html")):
        try:
            out[t.name] = ("ok", env.get_template(t.name).render(version="1.0.0", base=""))
        except Exception as e:  # the same failure before and after is the same outcome
            out[t.name] = ("error", type(e).__name__, str(e))
    return out


def test_a_page_rendered_before_and_after_write_is_the_same_bytes(tmp_path):
    source = ROOT / "src" / "tripbox" / "web" / "templates"
    before = tmp_path / "before" / "src" / "tripbox" / "web" / "templates"
    shutil.copytree(source, before)
    # the tree as it was before the header: the two lines taken off
    for t in before.glob("*.html"):
        lines = t.read_text(encoding="utf-8").splitlines(keepends=True)
        if lines[0].startswith("{# SPDX"):
            t.write_text("".join(lines[2:]), encoding="utf-8")
    after = tmp_path / "after" / "src" / "tripbox" / "web" / "templates"
    shutil.copytree(before, after)
    assert run(tmp_path / "after", "--write").returncode == 0
    assert (after / "base.html").read_text().startswith("{# SPDX")
    rendered = render_all(before)
    assert render_all(after) == rendered
    assert sum(1 for v in rendered.values() if v[0] == "ok") >= len(rendered) // 2
