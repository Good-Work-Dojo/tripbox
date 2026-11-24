# tools/verify

- `ci.sh` is the verification gate. It checks that `AGENTS.md` is at most 40,000 bytes and runs
  `check_workflows.py`, builds the `tripboxoss:test` image (the repository's `Dockerfile`, target
  `test`), runs `ruff check src tests` and `pytest -q` in it (pytest as uid 12345:54321, which the
  image has no account for), then `standalone_smoke.py` and the corpus scorecard (`tripbox eval
  --recorded`) as that user on a read-only root, then `ruff check tools` and `pytest -q -p
  no:cacheprovider tools` with the whole repository mounted read-only at `/repo`, then the demo
  site, and last the secret scan: gitleaks, its image pulled by digest, over the
  repository with no network, with its own rules and the allowances of `.gitleaks.toml` (each with
  its reason and its one path), every finding's value redacted. Each stage prints `== <name> ==`;
  `.github/workflows/ci.yml` runs the same stages under the same names, then `stack` and `browser`
  (below). The last line is `CI OK`. Docker is called with a label of the gate's own, a 6g memory
  cap and no network; `TRIPBOXOSS_DK`, an executable file, is called in its place when set
  (a maintainer's own wrapper). `TRIPBOXOSS_DK` and `TRIPBOXOSS_CI_LOCK` are for tests: when either
  is set, stderr says `ci.sh: overrides in effect: N`.
- `narrow.sh <pytest args>` runs one pytest selection in the existing image, with the repository
  mounted read-only at `/repo` as the working directory and `PYTHONPATH=/repo/src`. Paths are
  relative to the repository root. Run `ci.sh` first if the image is missing.
- `spdx.py [--write] [ROOT]` checks that every source file carries the two licence lines
  (`SPDX-License-Identifier: AGPL-3.0-only`, `Additional terms: see NOTICE`) in its own comment
  syntax, and that the Trips patch carries its own licence (GPL-3.0-only); `--write` adds the lines
  and changes no other byte. Data files carry none. `test_spdx.py` covers it. `ci.sh` runs it after
  the workflow check.
- `licences.py --collect` (inside the runtime image, no network) prints the installed Python
  distributions as JSON; `licences.py --dists FILE [--write]` checks each licence against an
  allow-list and holds the generated block of `THIRD_PARTY_NOTICES.md` to the package list. `ci.sh`
  builds the runtime target as `tripboxoss:rt` and runs both. `test_licences.py` covers it.
- `check_workflows.py [DIR]` checks `.github/workflows`: every `runs-on` is exactly `ubuntu-24.04`
  or `ubuntu-24.04-arm`, nothing mentions `self-hosted` or `pull_request_target`, and every
  `uses:` is a local path or one of GitHub's own actions (`actions/*`) pinned to a 40-hex commit.
  It prints `file:line: reason` per failure and exits 1. `test_check_workflows.py` covers it.
- `release_names.py` gives `.github/workflows/release.yml` what a release publishes: the image's
  name and tags (`names`), whether the tag is the package's version (`matches`), the notes from
  `CHANGELOG.md` (`notes`), the highest full release (`highest`), the image's labels (`labels`)
  and the digest a build pushed (`digest`). Its docstring gives each command; anything it refuses
  is one `release:` line and exit 2. `test_release_names.py` covers it.
- `outcome-diff.py <base.json> <head.json> --expect identical|refactor [--declared FILE]` compares
  two rehearsal outcome files by JSON path. `identical` fails on any difference; `refactor` fails
  on any difference no declared rule (`task<TAB>path-glob<TAB>from<TAB>to<TAB>reason`) covers. It
  prints the uncovered paths, never their values, then the counts; exit 0, 1 for uncovered
  differences, 3 when the message counts differ, 2 on a usage error, on two files that both hold
  no messages, or on a rule whose glob is made only of `*` and `**`.

- `standalone_smoke.py` is a new install end to end, run in a container of the test or the runtime
  image with no network, as any user. Its steps, in order: `doctor` (every optional feature off, no
  problem), `poll` (mail is off, exit 0), `worker` (`tripbox work` starts and the health check says
  `ok` within fifteen seconds; the child is stopped), `web` (the health check answers) and `page`
  (signed in through the login form, the first run's trips page). Every command it runs gets a
  password of its own as `TRIPBOX_PASSWORD` unless the environment sets one. The last line is
  `standalone smoke: ok` or `standalone smoke: FAIL <step>`.

- Three runners start a stack in one command where a workflow may start containers, each only when
  the switch its header names is set to 1 (without it they say what they would start and exit 2),
  and each takes its stack down again whatever happened. Each script's header gives its steps;
  the last line names the step that failed. `test_stack_scripts.py` covers them against a stub
  `docker`.
  - `stack_smoke.sh --image REF` is the quick start's default path as a stranger takes it: the
    bundle unpacked, `.env` the one password line, the stack started under the project
    `tripbox-smoke` on `127.0.0.1:8110` (refused when that port is in use), the page and `doctor`
    checked, the published port and the restart policies read back: `stack smoke: ok`.
  - `browser_checks.sh [--image REF] [--keep-out DIR]` runs the screenshot stack's four commands
    (below) over a demo seeded for the day and given covers (`screens/covers_seed.py`), under the
    project `tripbox-screens`: `browser checks: ok`. `--keep-out DIR` copies the pictures and the
    checks' summary (`shoot.txt`) to `DIR` (made with mode 700) before the stack comes down, so
    they can be looked at.
  - `flightlog_e2e.sh [--image REF]` takes the maintainers' own end-to-end runner's steps with plain
    `docker compose` under the project `tripbox-e2e`: `flight log e2e: ok`.

## screens/

A harness that photographs the trips page of any ledger. `screens/compose.yml` is a three-service
stack: `web` serves the page from a ledger directory behind its own password sign-in, `fresh`
serves a brand-new install's (the same image over an empty data directory on its tmpfs, behind
the same password and base path), and `browser` is a Chromium container that signs in to both. The
stack publishes no port, and the script blocks and records every request the page makes to another
origin, so the shots do not depend on the network, and records every request to the page's own
origin outside its base path. Run it from a directory that holds `compose.yml`, `shoot.py`,
`checks.py`, a ledger directory `demo/` (what `tripbox demo seed --out demo` writes, or any
directory with `data/ledger.db` and `config/`) and an empty `out/`. Five variables are read:
`SCREENS_TRIPBOX_IMAGE` (an image of the app), `SCREENS_BROWSER_IMAGE` (the image built from
`screens/Dockerfile`, a pinned Playwright base plus the matching Python package), `SCREENS_USER`
(the `uid:gid` that owns `demo/` and `out/`, default `1000:1000`), `SCREENS_BASE` (the base path the
page is served under, such as `/tripbox`; default none) and `SCREENS_PASSWORD` (the page's password,
which every service takes; default `harness-only-password`). Then run these from that same
directory, with `REPO` set to a checkout of this repository: build the browser image, start the
services (the browser waits for both pages, `fresh` started with it, and idles until a run is
executed in it), run `shoot.py` in the browser, and remove the stack:

    docker build -t "$SCREENS_BROWSER_IMAGE" "$REPO/tools/verify/screens"
    docker compose up -d --wait web browser
    docker compose exec -T browser python3 /harness/shoot.py --base-url "http://web:8110$SCREENS_BASE" --out /out
    docker compose down -v --remove-orphans

`shoot.py` signs in through the page's login form (the password is `--password`, else
`SCREENS_PASSWORD`; with none, the page is taken to have no login and the `protected` check
fails), finds the pages from the page itself (the index, `?past=all`, up to `--max-trips` trips,
a share page and the shared page of a link made through its form, opened with no cookie, the
edit page of one booking of each kind they hold, the first trip again with its ⋯ sheet open
(`sheet-open`, so the place and photo controls are in a shot), the three pages that add a
booking, an upload's status page, the deleted list and a missing trip, then the login page as it
shows and after a wrong password, and last `fresh`'s list as `first-run`, from `--fresh-url`,
else `SCREENS_FRESH_URL`, which the compose file gives the browser),
signs each new browser context in before it loads its page, writes full-page PNGs at 390
and 1280 pixels wide in the light and dark schemes to `out/`, measures horizontal overflow at 320
pixels, and records every request to another origin, and every one to its own origin outside the
base path, in `out/report.json`. The status page is the one the paste form lands on after the
browser sends one fixed line of text from it, so a run leaves that one upload, waiting, in the
ledger it shows (and the link, its share page and Settings list it). Once the pictures are taken,
`shoot.py` runs the eight checks of `checks.py`, each
an entry `{name, ok, detail}` in `report.json` whose detail names the page and the element or
request at fault: `csp` (no page reports a violation of its content policy, collected on every load
by an init script; a load without that listener leaves its page not looked at, and once a run an
inline style planted on the first page must be reported, else `the page's policy did not report a
planted inline style`), `other-origins` (no request to another origin), `outside-the-base` (no
request to its own origin outside the base path), `labels` (with every `<details>` open, no visible
input, select, textarea, button, summary or link without an accessible name, and every trip page
holds the photo file input and its Upload photo button), `escape` (each ⋯ sheet
of the first trip page opens with Enter, closes on Escape and leaves the focus on its ⋯),
`protected` (with no cookie, and again after a sign-out with the ended session's cookie sent back,
every page lands on the login page), `shared` (the shared page holds no script and asks for nothing
but itself, the stylesheet and the font, and is in no store of the service worker's once opened
signed in; a wrong link is a 404 with an empty body) and `offline` (in a context of its own whose
network a route cuts, `internetdisconnected`, after proving the cut reaches a request the service
worker itself makes, else `could not test: …`: the worker's scope is the base; the first trip,
opened online, reloads offline as its saved copy, with its title, the banner and every control
disabled and its cover photo loaded (`naturalWidth` above 0; `--expect-covers` makes a trip with no
cover a failure), photographed as `offline-copy` after the ledger's pages; a trip never opened
gives the no-copy page and a form posted from the copy the refusal; no store holds a page under `/login`,
`/s/`, `/settings` or `/add`; after a sign-out from Settings the trip has no copy and the pages
store is empty; Chromium's `Page.getAppManifest` reports no error). The summary ends `checks: N of M
ok`, then one `<name>: <detail>` line for each check that is not ok, which fails the run (`screens:
FAIL check <name>`), and `--fail-on-overflow` turns any overflow into a failure. The last line is
`screens: ok`.

`covers_seed.py LEDGER_DIR` gives a seeded demo ledger four covers, run in the app's image with no
network: pictures drawn with Pillow (a wide landscape and a tall portrait, no file from outside)
made into covers by the app's own image code, a ready Wikimedia cover with an invented credit on
the first trip to come, a ready owner cover on the trip under way, an owner upload still pending
on the latest past trip (no worker runs in the stack, so its note stays) and a failed one on the
trip before it. A shot's browser context counts a pending page's self-reloads as spent, so the page
stays as it loaded; the page's own reload is not what the shots show.

## proxy/

The stack the reverse-proxy recipes in `docs/proxy/` are run in. `proxy/compose.yml` holds a
fresh tripbox (`web` behind its own password sign-in and the `worker` that files what it is
handed, over one empty `data/`), a `proxy` reading one recipe as `proxy.conf`, and `probe`, idle
until a run executes `proxy/probe.py` in it. The stack publishes no port. `probe.py --proxy
http://proxy:80 --base-url URL --sample FILE` (password `PROXY_PASSWORD`) sends every request to
the proxy addressed to the base URL, walks `quickstart_check.py`'s steps with a share link, the
calendar feed, a 2 MiB upload and the service worker's scope between the trip and signing out,
then checks the bare base, the login page and its files, that every redirect and cookie stays
under the base path and that addresses outside it are not the page's. The last line is `proxy
probe: ok` or `proxy probe: FAIL <step>`.

## onehost/

AirTrail and tripbox at one hostname, behind `docs/proxy/caddy-airtrail.Caddyfile`.
`onehost/compose.yml` holds a throwaway AirTrail (`flightlog`, the image `ONEHOST_AIRTRAIL_IMAGE`
names, with its own Postgres in a tmpfs and its demo users), a fresh tripbox (`web` under
`/tripbox` and the `worker`, which sends the owner's flights to that AirTrail with the key
`TRIPBOX_AIRTRAIL_API_KEY`, dry run off), Caddy as `proxy`, an idle `probe`, and in the profile
`click` a `browser` (the screenshot stack's image). AirTrail publishes its port on loopback,
so its first start can fetch its airport table. Three scripts run in it:

- `probe.py --proxy http://proxy:80 --host flights.example.com --path /tripbox --sample FILE`
  (`ONEHOST_PASSWORD`, `ONEHOST_AIRTRAIL_KEY`) walks the hostname: AirTrail at the root,
  tripbox under the path with its cookie there, the neighbouring paths, the trips list's
  flight-log link, and a pasted booking reaching AirTrail. Last line `onehost probe: ok` or
  `onehost probe: FAIL <step>`.
- `served.py --url URL --trips URL [--host NAME]` (standard library; it works against any
  AirTrail) crawls AirTrail's scripts for the Trips button's link: `dock link: found`,
  `not found` or `FAIL <what>`.
- `click.py` signs in at AirTrail's demo door (`ONEHOST_DEMO_MODE=true`) and clicks the button
  in Chromium: `dock click: ok` or `dock click: FAIL <step>`.

## The rest of the folder

- `airtrail_test_key.py CONTAINER (--first-user | --second-user | --owner | --other-user |
  --user-id ID)` inserts the API key of an end-to-end run into a throwaway AirTrail's database the
  way AirTrail stores one (the base64 SHA-256 of the key, with its scopes), for one user chosen by
  id or as the instance's owner. The key comes from the variable `--key-env` names, never from an
  argument, and no line it prints holds it. `airtrail/compose.yml` is the stack `flightlog_e2e.sh`
  starts: a throwaway AirTrail, its Postgres and a container of the test image.
- `demo_site_check.py DIR` checks an exported demo site before it is published: every page holds
  the banner once and no script, no form action, no live control and no address that leaves the
  tree; `index.html` links a trip and `.nojekyll` exists. The last line is `demo site: ok <n>
  pages` or `demo site: FAIL <file>: <what>`.
- `readme_blocks.py README.md NAME` prints one of the README's named blocks (a fenced block after
  a `<!-- block: NAME -->` line); the quick start's runner and the README's tests read them, so
  what the README shows is what is run and checked.
- `screens/readme.py --site DIR --out DIR` photographs the README's four pictures from an exported
  demo site, in the screenshot harness's browser image, blocking every request to another origin;
  `screens/constraints.txt` pins the packages that image installs.

## Tests

The tests run in `ci.sh`'s tools stage (or one at a time through `narrow.sh`). Every name, address
and date in them is invented.

- `test_spdx.py`, `test_licences.py`, `test_check_workflows.py`, `test_release_names.py` and
  `test_outcome_diff.py` cover the script their name gives; `test_outcome_diff.py` is the only home
  of `outcome-diff.py`'s tests.
- `test_workflows.py` and `test_github_files.py` hold the workflows and the files GitHub reads
  (issue forms, pull-request template, contributor guide) to their rules; `test_ignore_files.py`
  holds what git and the build context keep out; `test_image_contents.py` holds the runtime image's
  contents.
- `test_docs.py` and `test_readme.py` hold the README and the guides to what is there: links,
  settings, commands, addresses and the blocks a stranger types.
- `test_demo_site_check.py`, `test_readme_shots.py` and `test_screens.py` cover the demo-site
  check, the README's picture runner and the screenshot harness; `test_quickstart_check.py`,
  `test_proxy.py`, `test_onehost.py`, `test_dock.py`, `test_airtrail_test_key.py` and
  `test_stack_scripts.py` run the checkers and runners against fake pages and stub tools.
- `test_shipped_names.py` holds the tree to two rules: no shipped file names a private tool of the
  maintainers' host (a short list of files, each with its reason, may), and every file of this
  folder is named here.
