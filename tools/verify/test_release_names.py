# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""What a release publishes (release_names.py, which .github/workflows/release.yml runs): the
image's name comes from the repository lower-cased, so the public repository and a fork publish
under different names; a full release is tagged with its version, <major>.<minor>, <major> and
latest unless a higher full release is already published, and a prerelease is a rehearsal tagged
with its version alone; the tag must be the package's version; the notes are CHANGELOG.md's
section for the version; the image's labels are the project's; and a build's digest is read from
its metadata file or refused."""

from __future__ import annotations

import io
import re
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(HERE))
import release_names as rn  # noqa: E402  (tools/verify is not a package)

SOURCE = "https://github.com/Good-Work-Dojo/tripbox"
REVISION = "3f" * 20


# ---------------------------------------------------------------- names and tags


@pytest.mark.parametrize(
    "repository, image",
    [
        ("Good-Work-Dojo/tripbox-rc", "ghcr.io/good-work-dojo/tripbox-rc"),
        ("Good-Work-Dojo/tripbox", "ghcr.io/good-work-dojo/tripbox"),
        ("Example-Org/Tripbox-Fork", "ghcr.io/example-org/tripbox-fork"),
        ("Someone/My.Fork_2", "ghcr.io/someone/my.fork_2"),
    ],
    ids=["the-working-repository", "the-public-one", "a-fork", "a-fork-of-another-spelling"],
)
def test_image_names_come_from_the_repository(repository, image):
    r = rn.names(repository, "v1.0.0")
    assert r.image == image and r.image == r.image.lower()
    fork, public = rn.names("Example-Org/Tripbox-Fork", "v1.0.0-rc.1"), rn.names(
        "Good-Work-Dojo/tripbox", "v1.0.0"
    )
    assert fork.image != public.image, "a fork's candidate never takes the public name"


def test_a_full_release_tags_its_version_its_minor_its_major_and_latest():
    r = rn.names("Good-Work-Dojo/tripbox", "v1.2.3")
    assert (r.version, r.tags, r.rehearsal) == ("1.2.3", ("1.2.3", "1.2", "1", "latest"), False)
    # the first release has nothing above it; the highest itself and a higher patch move them too
    for tag, top in (("v1.0.0", None), ("v1.2.0", "v1.2.0"), ("v1.2.1", "v1.2.0"),
                     ("v2.0.0", "v1.10.0"), ("v1.10.0", "v1.9.0")):
        major, minor = tag[1:].split(".")[:2]
        assert rn.names("a/b", tag, highest_full=top).tags == (
            tag[1:], f"{major}.{minor}", major, "latest"), tag


@pytest.mark.parametrize(
    "tag, prerelease",
    [("v1.0.0-rc.1", True), ("v1.0.0-rc.1", False), ("v1.2.0", True)],
    ids=["a-candidate-marked-so", "a-pre-release-part-whatever-the-flag", "a-release-marked-so"],
)
def test_a_prerelease_is_a_rehearsal_tagged_with_its_version_alone(tag, prerelease):
    r = rn.names("Good-Work-Dojo/tripbox", tag, prerelease=prerelease, highest_full="v0.9.0")
    assert (r.version, r.tags, r.rehearsal) == (tag[1:], (tag[1:],), True)


@pytest.mark.parametrize(
    "tag, top",
    [("v1.0.5", "v1.2.0"), ("v1.1.9", "v1.2.0"), ("v1.9.0", "v2.0.0")],
    ids=["an-older-line", "an-older-minor", "an-older-major"],
)
def test_a_patch_to_an_older_line_moves_no_shared_tag(tag, top):
    r = rn.names("Good-Work-Dojo/tripbox", tag, highest_full=top)
    assert (r.tags, r.rehearsal) == ((tag[1:],), False)


def test_the_highest_full_release_is_read_from_the_published_tags(capsys, monkeypatch):
    assert rn.highest(["v1.2.0", "v1.10.0", "v1.9.9", "v2.0.0-rc.1", "nightly", " v1.10.0 ", ""]) \
        == "v1.10.0"
    assert rn.highest(["v1.0.0-rc.1", "other"]) is None and rn.highest([]) is None
    with pytest.raises(ValueError, match="highest full release"):
        rn.names("a/b", "v1.0.0", highest_full="v1.1.0-rc.1")
    monkeypatch.setattr(rn.sys, "stdin", io.StringIO("v1.0.0\nv1.2.0\nv1.3.0-rc.1\n"))
    assert rn.main(["highest"]) == 0 and capsys.readouterr().out == "v1.2.0\n"
    monkeypatch.setattr(rn.sys, "stdin", io.StringIO(""))
    assert rn.main(["highest"]) == 0 and capsys.readouterr().out == ""


@pytest.mark.parametrize(
    "tag",
    ["1.0.0", "v1.0", "v1.0.0.0", "v01.0.0", "v1.0.0-", "v1.0.0+build.1", "v1.0.0-rc 1", "latest",
     "v1.0.0\n", "", "v1.0.0;x"],
)
def test_a_tag_of_another_form_is_refused(tag, capsys):
    with pytest.raises(ValueError, match="not of the form v1.2.3"):
        rn.names("Good-Work-Dojo/tripbox", tag)
    assert rn.main(["names", "Good-Work-Dojo/tripbox", tag]) == 2
    err = capsys.readouterr().err
    assert err.startswith("release: ") and len(err.splitlines()) == 1


@pytest.mark.parametrize(
    "repository", ["tripbox", "a/b/c", "-owner/name", "owner/na me", "owner/", ""]
)
def test_a_repository_of_another_form_is_refused(repository):
    with pytest.raises(ValueError, match="owner/name"):
        rn.names(repository, "v1.0.0")


def test_the_command_prints_the_outputs_one_a_line(capsys):
    assert rn.main(["names", "Example-Org/Tripbox-Fork", "v1.0.0-rc.1", "--prerelease"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "image=ghcr.io/example-org/tripbox-fork", "version=1.0.0-rc.1", "rehearsal=true",
        "tags=1.0.0-rc.1"]
    assert rn.main(["names", "Good-Work-Dojo/tripbox", "v1.0.5", "--highest-full", "v1.2.0"]) == 0
    assert capsys.readouterr().out.splitlines()[-2:] == ["rehearsal=false", "tags=1.0.5"]
    assert rn.main(["names", "Good-Work-Dojo/tripbox", "v1.0.0"]) == 0
    assert capsys.readouterr().out.splitlines()[-1] == "tags=1.0.0 1.0 1 latest"
    for argv in (["names", "a/b", "v1.0.0", "--highest-full"],
                 ["names", "a/b", "v1.0.0", "--other"],
                 ["names", "a/b", "v1.0.0", "--prerelease", "--prerelease"], ["highest", "x"],
                 ["other"], []):
        assert rn.main(argv) == 2, argv
        assert capsys.readouterr().err.startswith(("release:", "usage:"))


# ---------------------------------------------------------------- the package's version


def pyproject(tmp_path: Path, version: str) -> Path:
    p = tmp_path / "pyproject.toml"
    p.write_text(f'[project]\nname = "tripbox"\nversion = "{version}"\n')
    return p


def test_the_tag_must_be_the_packages_version(tmp_path, capsys):
    p = pyproject(tmp_path, "1.0.0")
    for tag in ("v1.0.0-rc.1", "v1.0.0"):
        assert rn.main(["matches", str(p), tag]) == 0, tag
        assert capsys.readouterr().out == ""
    assert rn.main(["matches", str(p), "v1.0.1"]) == 2
    assert capsys.readouterr().err == "release: the tag v1.0.1 is not the package's version 1.0.0\n"
    for bad in ("v1.0", "1.0.0"):
        assert rn.main(["matches", str(p), bad]) == 2
        assert "not of the form v1.2.3" in capsys.readouterr().err
    assert rn.main(["matches", str(tmp_path / "missing.toml"), "v1.0.0"]) == 2
    capsys.readouterr()
    # the repository's own tag is its version
    version = rn.package_version((ROOT / "pyproject.toml").read_text("utf-8"))
    rn.matches((ROOT / "pyproject.toml").read_text("utf-8"), f"v{version}")


def test_a_pyproject_without_a_version_is_refused(tmp_path):
    with pytest.raises(ValueError, match="no version"):
        rn.package_version('[project]\nname = "tripbox"\n')
    with pytest.raises(ValueError, match="cannot be read"):
        rn.package_version("[project\n")


# ---------------------------------------------------------------- notes

CHANGELOG = """# Changelog

Intro.

## [Unreleased]

- Something not yet released.

## [1.1.0-rc.2] - 2030-02-01

- A candidate's own notes.

## [1.0.0]

The first release.

### Added

- A thing.

[1.0.0]: https://example.net/releases/tag/v1.0.0
"""


@pytest.mark.parametrize(
    "changelog, words",
    [("# Changelog\n\n## [0.9.0]\n\n- Old.\n", "no section"),
     ("# Changelog\n\n## [1.0.0]\n\n## [0.9.0]\n\n- Old.\n", "is empty"),
     ("# Changelog\n\n## [1.0.0]\n\n   \n\n## [0.9.0]\n\n- Old.\n", "is empty")],
)
def test_notes_are_the_changelogs_section_and_an_empty_one_fails(changelog, words, tmp_path,
                                                                  capsys):
    for tag, first in (("v1.0.0", "The first release."), ("v1.0.0-rc.1", "The first release."),
                       ("v1.1.0-rc.2", "- A candidate's own notes.")):
        text = rn.notes(CHANGELOG, tag)
        assert text.splitlines()[0] == first and text.endswith("\n") and not text.endswith("\n\n")
        assert not [line for line in text.splitlines() if line.startswith("## ")] \
            and "https://example.net" not in text, \
            "up to the next section or the link definitions"
    with pytest.raises(ValueError, match=words):
        rn.notes(changelog, "v1.0.0")
    (tmp_path / "CHANGELOG.md").write_text(changelog)
    assert rn.main(["notes", str(tmp_path / "CHANGELOG.md"), "v1.0.0"]) == 2
    err = capsys.readouterr().err
    assert err.startswith("release: ") and len(err.splitlines()) == 1


def test_the_notes_of_this_repositorys_version_come_from_its_changelog():
    changelog = (ROOT / "CHANGELOG.md").read_text("utf-8")
    version = rn.package_version((ROOT / "pyproject.toml").read_text("utf-8"))
    final, candidate = rn.notes(changelog, f"v{version}"), rn.notes(changelog, f"v{version}-rc.1")
    assert final == candidate and re.search(r"^### ", final, re.M)
    dated = rf"^## \[{re.escape(version)}\] - \d{{4}}-\d{{2}}-\d{{2}}$"
    assert re.search(dated, changelog, re.M), \
        "the heading carries its release's date, as Keep a Changelog writes it"


# ---------------------------------------------------------------- labels and digests


def test_labels_are_the_projects_and_name_the_licence(capsys):
    meta = rn.labels(SOURCE, "1.0.0-rc.1", REVISION, "1893456000")  # 2030-01-01T00:00:00Z
    assert meta == {
        "org.opencontainers.image.title": "tripbox",
        "org.opencontainers.image.licenses": "AGPL-3.0-only",
        "org.opencontainers.image.source": SOURCE,
        "org.opencontainers.image.url": SOURCE,
        "org.opencontainers.image.documentation": SOURCE,
        "org.opencontainers.image.version": "1.0.0-rc.1",
        "org.opencontainers.image.revision": REVISION,
        "org.opencontainers.image.created": "2030-01-01T00:00:00Z",
    }
    args = rn.build_arguments(meta)
    assert args == [f"--label={k}={v}" for k, v in meta.items()] + [
        f"--annotation=manifest:{k}={v}" for k, v in meta.items()]
    assert len(args) == 16 and all("\n" not in a for a in args)
    assert rn.main(["labels", SOURCE, "1.0.0-rc.1", REVISION, "1893456000"]) == 0
    assert capsys.readouterr().out.splitlines() == args


@pytest.mark.parametrize(
    "source, version, revision, created, words",
    [
        ("http://github.com/a/b", "1.0.0", REVISION, "0", "https://host/owner/name"),
        ("https://github.com/a/b c", "1.0.0", REVISION, "0", "https://host/owner/name"),
        ("https://github.com/a/b\n--push", "1.0.0", REVISION, "0", "https://host/owner/name"),
        (SOURCE, "v1.0.0", REVISION, "0", "version"),
        (SOURCE, "1.0.0", "3f" * 19, "0", "40 hex"),
        (SOURCE, "1.0.0", "3F" * 20, "0", "40 hex"),
        (SOURCE, "1.0.0", REVISION, "-1", "seconds since the epoch"),
        (SOURCE, "1.0.0", REVISION, "2030-01-01", "seconds since the epoch"),
        (SOURCE, "1.0.0", REVISION, "", "seconds since the epoch"),
    ],
)
def test_labels_from_an_argument_of_another_form_are_refused(source, version, revision, created,
                                                             words):
    with pytest.raises(ValueError, match=words):
        rn.labels(source, version, revision, created)


def test_a_digest_of_another_form_fails(tmp_path, capsys):
    good = "sha256:" + "ab" * 32
    assert rn.digest(f'{{"containerimage.digest": "{good}", "image.name": "x"}}') == good
    for text in ("", "[]", "{}", '{"containerimage.digest": null}',
                 f'{{"containerimage.digest": "{good}x"}}',
                 '{"containerimage.digest": "sha256:abc"}',
                 f'{{"containerimage.digest": "{good.upper()}"}}',
                 f'{{"containerimage.config.digest": "{good}"}}'):
        with pytest.raises(ValueError, match="no image digest"):
            rn.digest(text)
    (tmp_path / "m.json").write_text(f'{{"containerimage.digest": "{good}"}}')
    assert rn.main(["digest", str(tmp_path / "m.json")]) == 0
    assert capsys.readouterr().out == good + "\n"
    (tmp_path / "bad.json").write_text("{}")
    for argv in (["digest", str(tmp_path / "bad.json")], ["digest", str(tmp_path / "none.json")],
                 ["labels", SOURCE, "1.0.0", REVISION], ["labels", SOURCE, "1.0.0", "x", "0"]):
        assert rn.main(argv) == 2, argv
        err = capsys.readouterr().err
        assert err.startswith(("release:", "usage:")) and len(err.splitlines()) == 1
