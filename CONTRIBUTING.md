# Contributing to tripbox

Thank you for helping. This page says how a change gets in. `docs/architecture.md` is the short, readable tour of how the parts fit; `AGENTS.md` is the
denser map of the code and its rules: read the part you are changing first.

## Never a real email, name or trip

Nothing real goes into an issue, a pull request, a test, a fixture, a screenshot or a commit message: no confirmation
email, booking code, ticket number, passenger name, address, or trip of yours or anyone else's. A confirmation email
is full of them, and a repository's history cannot be taken back.

- **A parse problem** (an email tripbox misreads or cannot read) is reported with the output of `tripbox report`,
  which prints a redacted sample of the email and what tripbox did with it. Read the sample before you post it, and
  take out anything the redactor missed; say so, so that the redactor can be fixed.
- **A new test email** is written from scratch, by the corpus's `tests/corpus/README.md` ("Writing a new email"):
  invented people from the Sample family, `.example` hosts, dates in 2027 and invented booking codes. Never a real
  email with its values changed, and never a trimmed one.
- **A pull request that holds real data** is closed, and its history has to be rewritten.

## The gate

`tools/verify/ci.sh` runs every check in Docker: the workflow rules, the licence headers, the dependency licences,
the lint, the tests of the app and of the tools, a new install as any user, the corpus scorecard, the demo site and a
secret scan (which pulls gitleaks's image). Your machine needs bash, git, tar and Docker, and no Python. The gate has been run on Linux
only; macOS and WSL2 on Windows are untried. It must end with `CI OK`, and the same checks run on every pull request. For a quick loop
on a few tests, `tools/verify/narrow.sh tests/test_x.py -k name` runs them in the image the gate last built.

To run a build of your working tree as an install, build the image from the repository
(`docker build --target runtime -t tripbox:local .`) and set `TRIPBOX_IMAGE=tripbox:local` in the `.env` of a
bundle unpacked elsewhere, so that `docker compose up -d` runs it.

A question has no form of its own: open an issue with the Feature form and start its title with "Question".

A change in behaviour comes with a test that fails without it. Never weaken an existing assertion to make a change
pass; if one is wrong, say why in the pull request.

## Words, comments and the header rule

- Words a person reads (a page, a check's message, a command's output) say what is wrong and what fixes it, in plain
  words.
- Comments and test titles say what the code does and why, in the present tense. No ticket, task or review numbers.
- Every source file starts with the two licence lines, and the gate checks them;
  `docker run --rm --network none --user "$(id -u):$(id -g)" -v "$PWD:/repo" -w /repo python:3.14-slim python tools/verify/spdx.py --write`
  adds them. A data file (JSON, an email, a text file) carries none.
- A new dependency must be under a licence the gate allows (`tools/verify/licences.py`: MIT, Apache-2.0, BSD, ISC and
  the like); no GPL, AGPL or LGPL at run time. Add its line to `THIRD_PARTY_NOTICES.md` in two steps:
  `docker build --target runtime -t tripbox:rt .`, then
  `docker run --rm --network none --user "$(id -u):$(id -g)" -v "$PWD:/repo" -w /repo tripbox:rt sh -c 'python tools/verify/licences.py --collect >/tmp/dists.json && python tools/verify/licences.py --dists /tmp/dists.json --write'`.
- tripbox is under the GNU Affero General Public License, version 3, with the additional terms in `NOTICE`. A change
  you make is under the same licence.

## The Contributor Licence Agreement

Before a first pull request is merged, its author signs `CLA.md` through CLA Assistant, a GitHub App set up on the
repository (pointed at a Gist of `CLA.md`), which asks on the pull request. You keep the copyright in
what you contribute; the agreement lets the project distribute it, including under other licences later (its
section 4 says how).

## How a release is cut

Maintainers only. A release is a `## [X.Y.Z]` section in `CHANGELOG.md`, the same number as `version` in
`pyproject.toml`, merged through a pull request; then `gh release create vX.Y.Z --target <the merge commit>` (the
script `tools/maintainer/post-publish.sh` runs it and checks the image and the demo site afterwards, see
`tools/maintainer/README.md`). The release's notes are the changelog section, and publishing it builds the image for
both architectures. A tag with a pre-release part (`vX.Y.Z-rc.1`) is a rehearsal that `release.yml` proves and
cleans up on its own; the script refuses one.

## Pull requests

Keep a pull request to one change, describe what it does and how you checked it. Security problems are never reported
in an issue or a pull request: see `SECURITY.md`. Everyone taking part follows the `CODE_OF_CONDUCT.md`.
