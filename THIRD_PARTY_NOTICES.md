# Third-party notices

tripbox is free software under the GNU Affero General Public License, version 3 (`LICENSE`, with the additional terms
in `NOTICE`). It uses the components below, each under its own licence, which this file keeps and does not change.
The image carries this file and `LICENSES/` beside `LICENSE` and `NOTICE`, at `/app`.

## kitinerary-extractor

A separate program from KDE's KItinerary project. The image carries it and the parser runs it as a process, reading
a message from a temporary file and printing its JSON; it is not linked into tripbox, and tripbox's
own code never includes any of it.

- **Licence.** KDE's source states `LGPL-2.0-or-later` in its source files (for instance the command-line tool's
  `src/cli/main.cpp`), `BSD-3-Clause` in its build files, `CC0-1.0` in some data and metadata files, and
  `ODbL-1.0` for its generated airport table (`src/lib/knowledgedb/airportdb_data.cpp`, from OpenStreetMap
  contributors, with Wikidata, which is `CC0-1.0`). Its repository's `LICENSES/` folder holds `BSD-2-Clause`,
  `BSD-3-Clause`, `CC0-1.0`, `LGPL-2.0-or-later` and `ODbL-1.0`. `LICENSES/LGPL-2.0-or-later.txt` and
  `LICENSES/ODbL-1.0.txt` here are KDE's own copies of the two that cover the program and its airport table.
  Debian's copyright file for its `kitinerary` package (24.12.3-1) says the same: `LGPL-2+` for the files not named
  otherwise, with `CC0-1.0`, `BSD-3-Clause` and `ODbL-1.0` for the files it names.
- **What the static build bundles.** The build KDE publishes is one static binary. KDE's scripts for it at the
  `v26.04.3` tag build Qt 6.10.2 (static), OpenSSL 3.6.1 and iso-codes 4.18.0, and its CI configuration names
  KDE Frameworks (`ki18n`, `kcontacts`, `kcalendarcore`, `kcodecs`, `kmime`), `kpkpass` and `zxing-cpp`. The
  archive holds the binary and its translation catalogues (`share/locale`) and no licence texts. As each project
  states its own licence: Qt 6 is offered under `LGPL-3.0-only` (as well as commercially and under the GPL);
  OpenSSL 3 and zxing-cpp under `Apache-2.0`; iso-codes under `LGPL-2.1-or-later`; and the KDE Frameworks named
  and `kpkpass` mostly under `LGPL-2.0-or-later`, with some files under `LGPL-2.1` or `LGPL-3.0` terms. Each
  project's own source is the authority for its files. `LICENSES/LGPL-3.0-only.txt` (which carries the GPL text
  it builds on), `LICENSES/LGPL-2.1-or-later.txt` and `LICENSES/Apache-2.0.txt` here are those licences' texts,
  from the SPDX License List (v3.27.0).
- **Version.** On amd64 the image carries KDE's build, 26.04.3 (a download pinned by its SHA-256 in the
  `Dockerfile`). KDE publishes it for x86_64 only, so on arm64 the image carries Debian 13's `libkitinerary-bin`,
  24.12.3-1, which links the system's own libraries. The build argument `KITINERARY_SOURCE`
  chooses either build where both exist.
- **Source.** KDE's repository is <https://invent.kde.org/pim/kitinerary> (the `v26.04.3` tag is the source of the
  amd64 build); Debian's source package is `kitinerary`, version 24.12.3-1 (<https://packages.debian.org/trixie/libkitinerary-bin>
  names it and links its copyright file). Anyone who receives the image should be able to fetch either from
  there; that Debian's source page serves the package's source was not verified.
- **Written offer of the source.** For at least three years after the last image that carries a given build of
  `kitinerary-extractor` is published, the tripbox maintainers will give anyone who asks a copy of the complete
  corresponding source of that build and of the libraries it bundles, as a download or on a medium customarily
  used for software interchange, for no more than the cost of providing it. To ask, open an issue at
  <https://github.com/Good-Work-Dojo/tripbox/issues> titled "Source request: kitinerary-extractor" and name the
  image tag. The same source is published by its own projects at the places listed under **Source**.

## Public Sans

The font the pages use: Public Sans, release v2.001 (GitHub's release list for
<https://github.com/uswds/public-sans> has the tag; the four bundled files are byte-identical to the `.woff2` files
of that release's archive, checked by SHA-256), four files in `src/tripbox/web/static/fonts/`
(`PublicSans-Regular.woff2`, `PublicSans-Medium.woff2`, `PublicSans-SemiBold.woff2`, `PublicSans-Bold.woff2`), served
with the pages. Copyright 2015 The Public Sans Project Authors (<https://github.com/uswds/public-sans>), under the
SIL Open Font License, version 1.1: `LICENSES/OFL-1.1.txt`, and in the folder with the files, `OFL.txt`.

## airportsdata

The Python package `airportsdata` (MIT; its project is <https://github.com/mborsetti/airportsdata>) is the
airport table the parser and the page read: codes, names, places and time zones. Its README says that the time zone
was originally sourced from TimeZoneDB (<https://timezonedb.com>) and that its other fields follow ISO 3166, ICAO and
IATA codes. Anything more about where its rows come from is its project's to say; it is not verified here.

## AirTrail

AirTrail (<https://github.com/johanohly/AirTrail>) is a separate program, GNU General Public License, version 3
(`LICENSES/GPL-3.0-only.txt`). tripbox can write flights to an AirTrail the user runs; it does not include,
link or ship any of AirTrail. `integrations/airtrail/trips-dock.patch` modifies AirTrail (one more button in its dock,
to the trips page) and is offered under AirTrail's licence, GPL-3.0-only, as the patch says. It is not part of the
app, and it is not in the image.

## Python, uv and the Debian base

The image is built on the official `python:3.14-slim` image, which is Debian 13 with Python 3.14. Python is under the
Python Software Foundation licence (`/usr/local/lib/python3.14/LICENSE.txt` in the image); uv, which installs the
dependencies below, is `MIT OR Apache-2.0`. Debian's packages each keep their copyright file under
`/usr/share/doc/<package>/copyright` in the image.

## Python packages

The packages the image installs at run time, one line each (name, licence, home page), checked against the
allow-list by `tools/verify/licences.py`. The audit reads the packages of the image built where it runs, which in
CI is the amd64 image; the arm64 image installs the same lock, and CI does not read its packages. A package under another licence cannot be added. The list is generated:
`tools/verify/licences.py --dists FILE --write` rewrites it, and nothing else in this file, from the JSON that
`licences.py --collect` prints inside the runtime image (`CONTRIBUTING.md` gives the commands).

<!-- licences:begin -->
- airportsdata — MIT — https://github.com/mborsetti/airportsdata/
- annotated-types — MIT — https://github.com/annotated-types/annotated-types
- anthropic — Apache-2.0 — https://github.com/anthropics/anthropic-sdk-python
- anyio — MIT — https://pypi.org/project/anyio/
- cffi — MIT-0 — https://pypi.org/project/cffi/
- click — BSD-3-Clause — https://github.com/pallets/click/
- cryptography — Apache-2.0 OR BSD-3-Clause — https://github.com/pyca/cryptography
- docstring_parser — MIT — https://github.com/rr-/docstring_parser
- h11 — MIT — https://github.com/python-hyper/h11
- httpcore2 — BSD-3-Clause — https://github.com/pydantic/httpx2
- httpx2 — BSD-3-Clause — https://github.com/pydantic/httpx2
- idna — BSD-3-Clause — https://github.com/kjd/idna
- imap-tools — Apache-2.0 — https://github.com/ikvk/imap_tools
- Jinja2 — BSD-3-Clause — https://github.com/pallets/jinja/
- jiter — MIT — https://github.com/pydantic/jiter/
- MarkupSafe — BSD-3-Clause — https://github.com/pallets/markupsafe/
- openai — Apache-2.0 — https://github.com/openai/openai-python
- opentelemetry-api — Apache-2.0 — https://github.com/open-telemetry/opentelemetry-python/tree/main/opentelemetry-api
- pycparser — BSD-3-Clause — https://github.com/eliben/pycparser
- pydantic — MIT — https://github.com/pydantic/pydantic
- pydantic_core — MIT — https://github.com/pydantic/pydantic
- PyJWT — MIT — https://github.com/jpadilla/pyjwt
- python-multipart — Apache-2.0 — https://github.com/Kludex/python-multipart
- PyYAML — MIT — https://pyyaml.org/
- sniffio — MIT OR Apache-2.0 — https://github.com/python-trio/sniffio
- starlette — BSD-3-Clause — https://github.com/Kludex/starlette
- truststore — MIT — https://github.com/sethmlarson/truststore
- typing-inspection — MIT — https://github.com/pydantic/typing-inspection
- typing_extensions — PSF-2.0 — https://github.com/python/typing_extensions
- tzdata — Apache-2.0 — https://github.com/python/tzdata
- uvicorn — BSD-3-Clause — https://uvicorn.dev/
<!-- licences:end -->
