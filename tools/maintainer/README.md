# Maintainer scripts

Two bash scripts for the repository's maintainer, run from a checkout of the repository. Neither is
part of the app or its image. Both talk to GitHub through `gh`, which holds its own token: neither
script reads or prints one. `install_bundle.py` is the third file here; it builds the quick start's
install bundle and has its own header and tests.

What they need:

- `gh`, signed in (`gh auth status`) as an account with **admin** rights on the repository.
  `adoption-report.sh` needs only read access, though its traffic figures need push access and
  show as unavailable without it.
- For `post-publish.sh`: also bash 4.4 or later (macOS's own `/bin/bash` is 3.2: install a newer
  one, for example with Homebrew, and the script refuses an older one), `docker`, `curl` and
  `python3` (3.12 or later, for the publish gate). For the first release only, a denylist you
  supply to the publish gate (`tools/publish_gate.py`), or `--skip-gate`. The denylist stays on
  the maintainer's machine: its path is given on the command line and never written into the
  tree.

## post-publish.sh

Run once after the repository is made public, and again for each later release. The first run
turns on what a public repository gets for free, publishes the first release and the demo site,
and checks what was published; a later run finds every switch as wanted, publishes its release and
checks it. In order, stopping at the first failure with a line that says what to do:

1. Checks, before anything changes, that the repository is public; that its default branch's head
   is the expected commit and that this checkout is at that head with no change in it (the version,
   the notes and the `ci.yml` job are read from it); that the version is a full release (a
   prerelease is refused: release candidates are proven by `release.yml` alone, which removes its
   own images), the same as `pyproject.toml`'s, with a `CHANGELOG.md` section; and that a tag or
   release of that version, if one exists, is on this commit. It reads the published full
   releases: with none but this version's, this is the first release. A private repository stops
   the script here too.
2. Turns on Dependabot alerts and security updates, secret scanning and push protection, private
   vulnerability reporting and CodeQL's default setup.
3. Applies a branch ruleset named `default-branch` to the default branch: the job of
   `.github/workflows/ci.yml` is required (its `name:`, else its id, read from the file; it must
   be reported by GitHub Actions), and force-pushes and deletion are refused. A ruleset of that
   name that says something else is brought back in line, so keep other rules in a ruleset of
   their own.
4. Sets the repository variable that `pages.yml` is gated on to `true` (the demo site's workflow
   runs only while it is), switches Pages on with GitHub Actions as its build, lets tags matching
   `v*` deploy from the `github-pages` environment (the workflow also runs when a release is
   published, from the release's tag, and every Monday from the latest full release, so the
   demo's dates follow the calendar) and runs `pages.yml` on the default branch.
5. Creates the release at the head commit with `gh release create --target <sha>`, its notes the
   version's section of `CHANGELOG.md`, and waits for `release.yml` to build and publish the image.
   A release below the highest full release (a patch to an older line) gets `--latest=false`, so
   GitHub's latest release, which the quick start's download and the weekly demo follow, stays the
   highest.
6. Pulls the image by its full version (`ghcr.io/<owner>/<name>:1.0.0` for `v1.0.0`, the one tag
   every release pushes) with an empty docker configuration, so without any sign-in, which proves it
   is public; it must hold `linux/amd64` and `linux/arm64`. Then the publish gate scans it with the
   denylist and prints counts and paths only. The first release needs `--denylist` or `--skip-gate`;
   a later release without `--denylist` is not scanned, and the step says so. Its token and pattern
   matches count only in the project's own files: `/app`, and inside its virtual environment
   `/app/.venv` only what tripbox installs of itself (its `tripbox-*` metadata, its `.pth` file and
   `bin/tripbox`). Everything else in the image is public upstream bytes the project did not write
   (the Debian base, the bundled kitinerary binary, pip, uv and the third-party Python packages),
   where author addresses, common words and public hosts can match the denylist's words and the
   generic patterns; those content matches are printed as one line, `upstream: N findings in M
   members (public upstream bytes, not counted)`, with no tokens, labels or paths, and never fail
   the step. A source map, a file named like an export file or a damaged archive fails it wherever
   it is. The rules are `MEMBER_RULES` in the script. It scans the image of this computer's
   architecture alone; the other's project files under `/app` are the same, its system packages are
   not read. When the anonymous pull fails, it prints the package's settings page, where its
   visibility is set.
7. Fetches the demo site: it must answer 200, and its page must hold the banner and carry its
   content-security policy in a `<meta>` tag.
8. The maintainers' own site's tripbox page (a separate repository; without one, skip this step).
   With `--site DIR` (a clone of the site's repository) and `--site-bundle FILE` (the page's branch,
   `product/tripbox`, saved as a bundle) the script makes the branch from the bundle (a local
   `product/tripbox` that is only an older copy is fast-forwarded to it; one with commits the bundle
   lacks is refused, with what to do), merges `origin/product/tripbox` into it when that moved (a
   merge commit marked `[CF-Pages-Skip]`; never a rebase), runs the site's `python3
   tools/check_site.py --no-provisional`, pushes the branch (never forced) and opens the pull
   request into `main`, or reports the open one. **It never merges the pull request**: merging is
   going live, and yours to do, as a merge commit whose message does not start `[CF-Pages-Skip]`,
   followed by the site's live check. Every check is made in step 1, before anything changes: that
   every fetch and push address of the clone's `origin` is the site's repository and it is clean;
   that no pull request of the branch is merged already (then the page is live and step 8 does
   nothing); `git fetch origin` in the clone (it changes only the clone's own refs); that the base
   site is on `main` (`git cat-file -e origin/main:site/assets/analytics.js`; if not, it stops and
   tells you); that the bundle verifies, holds the branch and its tip's message starts
   `[CF-Pages-Skip]` (a pushed branch without it gets a public preview); and that a moved remote
   would merge without a conflict. Without `--site` the step prints what to run by hand for the
   first release, and for a later one only that the page is live already; the script still exits
   0.

Each step reads the current state first and changes nothing that is already as wanted, so running
it again after a failure is safe: the steps already done are skipped.

**One-time manual step** (no API call makes it; the first release's last line is a reminder):
install the CLA Assistant GitHub App on the repository (from `https://cla-assistant.io`, signed in
as the repository's admin) and point it at a Gist holding `CLA.md`, so it asks the author of each
first pull request to sign (`CONTRIBUTING.md`).

| Flag | What |
|---|---|
| `--repo OWNER/NAME` | The repository (default `Good-Work-Dojo/tripbox`) |
| `--version vX.Y.Z` | The release (default: the first section of `CHANGELOG.md` headed by a version) |
| `--expect-sha SHA` | The commit the default branch and this checkout must be at; without it the checkout must equal the default branch's head |
| `--denylist DIR` | The publish gate's denylist directory; the first release needs it unless `--skip-gate`, a later one is scanned only with it |
| `--skip-gate` | Leave out the scan of the published image |
| `--export-names FILE` | File names the gate refuses in the image, one a line (default: none) |
| `--timeout MINUTES` | How long to wait for each workflow run (default 60) |
| `--site DIR` | A clone of the maintainers' site's repository, for step 8 (with `--site-bundle`) |
| `--site-bundle FILE` | The bundle of the site's `product/tripbox` branch |
| `--site-repo OWNER/NAME` | The site's repository (default `Good-Work-Dojo/GoodWorkDojoLanding`) |
| `--dry-run` | Make the reads and print every call that would change something, the pull, the scan and the fetch, without making them (the site's clone is still fetched, in step 1) |

```sh
tools/maintainer/post-publish.sh --denylist /path/to/denylist --dry-run
tools/maintainer/post-publish.sh --denylist /path/to/denylist --expect-sha <the commit>
```

Exit 0 when everything is done, 1 when a step failed, 2 on a usage error.

### Cutting a release after the first

1. Add a `## [X.Y.Z] - YYYY-MM-DD` section to `CHANGELOG.md` (the day it is released) and set
   `version` in `pyproject.toml` to the same number; both land on the default branch through a
   pull request.
2. Run `tools/maintainer/post-publish.sh --version vX.Y.Z --expect-sha <the merge commit>`
   (add `--dry-run` first to see what it would do): every switch is already as wanted, so it
   creates the release with `gh release create vX.Y.Z --target <sha>` (with `--latest=false` for a
   patch to an older line), waits for `release.yml`, pulls the image anonymously and checks both
   architectures, and checks the demo site. It needs no denylist (the image is scanned only when
   one is given) and repeats no launch step: no site page, no CLA reminder.
3. This script is for full releases only. A prerelease (`vX.Y.Z-rc.1`) is a rehearsal that
   `release.yml` proves and cleans up by itself, leaving no image behind; the script refuses one.

### Pins Dependabot cannot see

Dependabot updates the actions, the base images and the Python packages. These pins it does not
read, or is told to leave alone, so they age silently: look at each project's releases now and
then, and bump them by hand.

- The gitleaks image of the secret scan (`GITLEAKS` in `ci.yml` and `tools/verify/ci.sh`, the same
  digest in both) and the BuildKit image of the release builds (`BUILDKIT` in `release.yml`):
  images named in a workflow's or a script's own text.
- KDE's kitinerary-extractor build (`KITINERARY_VERSION` and `KITINERARY_SHA256` in the
  `Dockerfile`): a download, not an image. Bump both together, then check the corpus scorecard.
- Python's minor and major version: Dependabot brings only new digests of `python:3.14-slim`. A
  new Python moves the `Dockerfile`'s `FROM`, `.python-version` and ruff's `target-version` in
  `pyproject.toml` together, with `uv.lock` regenerated. `tools/verify/ci.sh` reads its Python
  image from the `Dockerfile`'s `FROM` line, so it follows on its own.
- The screenshot browser's Playwright version: Dependabot brings only new digests of its tag. A
  new version changes three places together: the `FROM` tag in `tools/verify/screens/Dockerfile`,
  the `playwright==` pin on its `pip install` line, and `playwright==` in
  `tools/verify/screens/constraints.txt`.

## adoption-report.sh

How the project is being taken up, read from GitHub alone (the app and the demo site carry no
analytics): stars, forks and watchers; the last 14 days' views and clones, total and unique; the
top referrers and the most viewed paths; open and closed issues and pull requests; and a line
saying that image pulls are not available through GitHub's API, with the package's address. Every
call is a read.

| Flag | What |
|---|---|
| `--repo OWNER/NAME` | The repository (default `Good-Work-Dojo/tripbox`) |
| `--json` | One JSON object instead of the text report |

GitHub keeps traffic for 14 days only, so a longer record means running it on a schedule and
keeping its `--json` output.
