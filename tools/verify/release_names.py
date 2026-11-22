#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""What a release publishes, for .github/workflows/release.yml: the image name and tags a git tag
gives, whether the tag is the package's version, the release's notes from CHANGELOG.md, the
image's labels and annotations, and the digest a build pushed.

  names REPOSITORY TAG [--prerelease] [--highest-full TAG]
      prints `key=value` lines (the workflow appends them to its step outputs):
        image      ghcr.io/<REPOSITORY lower-cased>: a registry's names are lower case, so
                   Good-Work-Dojo/tripbox publishes ghcr.io/good-work-dojo/tripbox, and a
                   repository of another name (a fork) publishes under its own name
        version    TAG without its leading v (v1.0.0 -> 1.0.0, v1.0.0-rc.1 -> 1.0.0-rc.1)
        rehearsal  true for a prerelease (--prerelease, which the workflow passes when the
                   release is marked so, or a version with a pre-release part such as -rc.1),
                   whose images the workflow removes again once they are proven; else false
        tags       the image tags, space-separated: the version, and for a full release also
                   <major>.<minor>, <major> and latest, as long as it is at or above
                   --highest-full, the highest full release already published (a patch to an
                   older line, v1.0.5 after v1.2.0, gets its version alone, so no shared tag
                   moves back). A prerelease is tagged with its own version alone.
  matches PYPROJECT TAG
      the package's version (pyproject.toml's project.version) is TAG without its v and without
      a pre-release part (v1.0.0-rc.1 and v1.0.0 are both 1.0.0's); prints nothing.
  notes CHANGELOG TAG
      prints the CHANGELOG's section for the tag's version (`## [1.0.0-rc.1]`), else for the
      version it leads to (`## [1.0.0]`): the lines under the heading up to the next `## `
      heading or the link definitions at the end.
  highest
      reads tags from standard input, one a line, and prints the highest full release among
      them (no pre-release part), or nothing when there is none; a line of another form is
      passed over.
  labels SOURCE VERSION REVISION CREATED
      prints the `docker buildx build` arguments that make the image's labels and its manifest's
      annotations the project's, one argument a line: `--label=<key>=<value>` and
      `--annotation=manifest:<key>=<value>` for each OCI key (title, licenses, source, url,
      documentation, version, revision, created). SOURCE is the repository's https address;
      VERSION the release's version (the tag without its v); REVISION the commit built (40 hex);
      CREATED its commit time in seconds since the epoch, written as UTC.
  digest METADATA
      prints the pushed image's digest from a `docker buildx build --metadata-file` file
      (`containerimage.digest`, `sha256:` and 64 hex).

TAG is `v<major>.<minor>.<patch>` with an optional pre-release part (`-rc.1`); REPOSITORY is
`owner/name`. Anything else, a tag that is not the package's version, a CHANGELOG with no such
section or an empty one, or a metadata file with no digest prints one line, `release: <what is
wrong>`, on standard error and exits 2. Standard library only."""

from __future__ import annotations

import json
import re
import sys
import tomllib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

REGISTRY = "ghcr.io"
_TAG = re.compile(
    r"v(?P<major>0|[1-9][0-9]*)\.(?P<minor>0|[1-9][0-9]*)\.(?P<patch>0|[1-9][0-9]*)"
    r"(?:-(?P<pre>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
)
_REPOSITORY = re.compile(r"[a-z0-9][a-z0-9-]*/[a-z0-9_][a-z0-9._-]*")
_HEADING = re.compile(r"## \[(?P<version>[^\]]+)\](?:\s.*)?")
_LINK = re.compile(r"\[[^\]]+\]: \S+")
_SOURCE = re.compile(r"https://[A-Za-z0-9.-]+(?::[0-9]+)?/[A-Za-z0-9._-]+/[A-Za-z0-9._-]+")
_REVISION = re.compile(r"[0-9a-f]{40}")
_EPOCH = re.compile(r"0|[1-9][0-9]{0,10}")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")

TITLE = "tripbox"
LICENCE = "AGPL-3.0-only"


@dataclass(frozen=True)
class Release:
    image: str
    version: str
    rehearsal: bool
    tags: tuple[str, ...]


def _tag(tag: str) -> re.Match:
    m = _TAG.fullmatch(tag)
    if not m:
        raise ValueError(f"the tag {ascii(tag)} is not of the form v1.2.3 or v1.2.3-rc.1")
    return m


def _number(m: re.Match) -> tuple[int, int, int]:
    return int(m.group("major")), int(m.group("minor")), int(m.group("patch"))


def highest(tags: list[str]) -> str | None:
    """the highest full release (no pre-release part) among `tags`, None when there is none"""
    full = [
        (_number(m), t.strip())
        for t in tags
        if (m := _TAG.fullmatch(t.strip())) and m.group("pre") is None
    ]
    return max(full)[1] if full else None


def names(
    repository: str, tag: str, prerelease: bool = False, highest_full: str | None = None
) -> Release:
    """the image, version, rehearsal and tags the release of `tag` from `repository` publishes;
    `highest_full` is the highest full release already published, which the shared tags never
    move below"""
    m = _tag(tag)
    repo = repository.lower()
    if not _REPOSITORY.fullmatch(repo) or len(repo) > 200:
        raise ValueError(f"the repository {ascii(repository)} is not of the form owner/name")
    top = None
    if highest_full is not None:
        top = _TAG.fullmatch(highest_full)
        if not top or top.group("pre") is not None:
            raise ValueError(
                f"the highest full release {ascii(highest_full)} is not of the form v1.2.3"
            )
    version = tag[1:]
    rehearsal = prerelease or m.group("pre") is not None
    shared = not rehearsal and (top is None or _number(m) >= _number(top))
    major, minor = m.group("major"), m.group("minor")
    tags = (version, f"{major}.{minor}", major, "latest") if shared else (version,)
    return Release(f"{REGISTRY}/{repo}", version, rehearsal, tags)


def package_version(pyproject: str) -> str:
    """project.version of a pyproject.toml's text"""
    try:
        version = tomllib.loads(pyproject).get("project", {}).get("version")
    except (tomllib.TOMLDecodeError, AttributeError) as e:
        raise ValueError("the pyproject file cannot be read") from e
    if not isinstance(version, str) or not version:
        raise ValueError("the pyproject file gives no version")
    return version


def matches(pyproject: str, tag: str) -> None:
    """raises unless `tag`, without its v and its pre-release part, is the package's version"""
    m = _tag(tag)
    version = package_version(pyproject)
    if f"{m.group('major')}.{m.group('minor')}.{m.group('patch')}" != version:
        raise ValueError(f"the tag {tag} is not the package's version {version}")


def notes(changelog: str, tag: str) -> str:
    """the changelog's section for `tag`, or for the version a prerelease of it leads to"""
    m = _tag(tag)
    version = tag[1:]
    lines = changelog.splitlines()
    starts = {
        h.group("version"): i for i, line in enumerate(lines) if (h := _HEADING.fullmatch(line))
    }
    base = f"{m.group('major')}.{m.group('minor')}.{m.group('patch')}"
    at = starts.get(version, starts.get(base))
    if at is None:
        raise ValueError(f"the changelog has no section `## [{version}]` or `## [{base}]`")
    body = []
    for line in lines[at + 1 :]:
        if line.startswith("## ") or _LINK.fullmatch(line):
            break
        body.append(line)
    text = "\n".join(body).strip("\n")
    if not text.strip():
        raise ValueError(f"the changelog's section for {version} is empty")
    return text + "\n"


def labels(source: str, version: str, revision: str, created: str) -> dict[str, str]:
    """the image's OCI keys and values: the project's title and licence, the repository's address
    as its source, url and documentation, the release's version, and the commit built and its
    commit time"""
    if not _SOURCE.fullmatch(source):
        raise ValueError(f"the source {ascii(source)} is not of the form https://host/owner/name")
    if not _TAG.fullmatch(f"v{version}"):
        raise ValueError(f"the version {ascii(version)} is not of the form 1.2.3 or 1.2.3-rc.1")
    if not _REVISION.fullmatch(revision):
        raise ValueError(f"the revision {ascii(revision)} is not a commit's 40 hex digits")
    if not _EPOCH.fullmatch(created):
        raise ValueError(
            f"the commit time {ascii(created)} is not a count of seconds since the epoch"
        )
    when = datetime.fromtimestamp(int(created), UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        f"org.opencontainers.image.{k}": v
        for k, v in (
            ("title", TITLE),
            ("licenses", LICENCE),
            ("source", source),
            ("url", source),
            ("documentation", source),
            ("version", version),
            ("revision", revision),
            ("created", when),
        )
    }


def build_arguments(meta: dict[str, str]) -> list[str]:
    """`docker buildx build` arguments giving the image these labels and its manifest these
    annotations"""
    return [f"--label={k}={v}" for k, v in meta.items()] + [
        f"--annotation=manifest:{k}={v}" for k, v in meta.items()
    ]


def digest(metadata: str) -> str:
    """the pushed image's digest from a `docker buildx build --metadata-file` file's text"""
    try:
        found = json.loads(metadata).get("containerimage.digest")
    except (ValueError, AttributeError):
        found = None
    if not isinstance(found, str) or not _DIGEST.fullmatch(found):
        raise ValueError(
            "the build's metadata file gives no image digest of the form sha256:<64 hex>"
        )
    return found


def _names(args: list[str]) -> Release | None:
    """`names REPOSITORY TAG [--prerelease] [--highest-full TAG]`, each flag at most once"""
    rest, prerelease, top = args[2:], False, None
    while rest:
        if rest[0] == "--prerelease" and not prerelease:
            prerelease, rest = True, rest[1:]
        elif rest[0] == "--highest-full" and len(rest) >= 2 and top is None:
            top, rest = rest[1], rest[2:]
        else:
            return None
    return names(args[0], args[1], prerelease=prerelease, highest_full=top)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    try:
        if len(args) >= 3 and args[0] == "names" and (r := _names(args[1:])) is not None:
            print(f"image={r.image}\nversion={r.version}\nrehearsal={str(r.rehearsal).lower()}")
            print(f"tags={' '.join(r.tags)}")
            return 0
        if len(args) == 3 and args[0] == "matches":
            matches(Path(args[1]).read_text("utf-8"), args[2])
            return 0
        if len(args) == 3 and args[0] == "notes":
            print(notes(Path(args[1]).read_text("utf-8"), args[2]), end="")
            return 0
        if args == ["highest"]:
            found = highest(sys.stdin.read().splitlines())
            if found:
                print(found)
            return 0
        if len(args) == 5 and args[0] == "labels":
            print("\n".join(build_arguments(labels(*args[1:]))))
            return 0
        if len(args) == 2 and args[0] == "digest":
            print(digest(Path(args[1]).read_text("utf-8")))
            return 0
    except (ValueError, OSError) as e:
        print(f"release: {str(e).splitlines()[0] if str(e) else type(e).__name__}", file=sys.stderr)
        return 2
    print(
        "usage: release_names.py names REPOSITORY TAG [--prerelease] [--highest-full TAG]"
        " | matches PYPROJECT TAG | notes CHANGELOG TAG | highest"
        " | labels SOURCE VERSION REVISION CREATED | digest METADATA",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
