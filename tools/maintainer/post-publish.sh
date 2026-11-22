#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
# Run once by the maintainer, after the repository is made public, from a checkout of it (bash). In order, stopping at
# the first failure with what to do next:
#   1. the repository is public; its default branch's head is the expected commit (--expect-sha), and this checkout is
#      at that head with no change in it; the version (a full release: a prerelease is refused, release.yml alone
#      proves a candidate), its CHANGELOG notes, ci.yml's job and the tag are checked. A private repository, and any
#      failure here, stops the script before anything changes;
#   2. Dependabot alerts and security updates, secret scanning and push protection, private vulnerability reporting
#      and CodeQL's default setup are switched on;
#   3. a branch ruleset on the default branch requires the job of .github/workflows/ci.yml (its name, read from the
#      file, reported by GitHub Actions) and forbids force-pushes and deletion; a ruleset of the same name is brought
#      in line in place;
#   4. the repository variable TRIPBOX_PAGES is set to true, Pages is switched on with the Actions build type, the
#      github-pages environment lets v* tags deploy as well as the default branch (pages.yml also runs on a
#      published release) and pages.yml is run on the default branch;
#   5. the release of the version (CHANGELOG.md's first versioned section unless --version says) is created at the
#      head with `gh release create --target`, its notes that section (tools/verify/release_names.py notes), and
#      release.yml's run is awaited;
#   6. the image is pulled by its full version (the one tag every release pushes, a patch to an older line included)
#      with an empty docker configuration, so without any sign-in, and must hold both architectures; the publish gate
#      (tools/publish_gate.py images) scans it with the given denylist and prints counts and paths only. Its content
#      matches count only in the project's own files (MEMBER_RULES): those in what the image holds from upstream (the base
#      image, the bundled extractor, pip, uv and the third-party packages) are reported as one count of public upstream
#      bytes; a source map, an export file's name or a damaged archive there still fails the step. When the
#      pull fails, the package's visibility settings page is printed;
#   7. the demo site answers 200, its page holds the banner and carries its content-security policy in a <meta> tag;
#   8. with --site DIR (a clone of the maintainers' site's repository) and --site-bundle FILE (the tripbox page's branch,
#      saved as a bundle): the bundle's product/tripbox becomes the local branch, origin/product/tripbox is merged
#      into it when the remote moved (a merge commit marked [CF-Pages-Skip]; never a rebase), the site's checker
#      runs, the branch is pushed (never forced) and the pull request into main is opened, or the open one
#      reported. It never merges: that is going live, and the owner's. Every check is made in step 1: the clone and
#      its remote, a pull request of the branch already merged (the page is live: nothing to do), a fetch of the
#      clone, the base site on the site's main, the bundle and its tip's [CF-Pages-Skip] marker, and that a moved
#      remote merges without conflict. Without --site it prints what to run by hand.
# Every step reads the state first and changes nothing that is already as wanted, so running the script again is
# safe. --dry-run makes the reads and prints every call that would change something (and the pull, the gate and the
# demo check) without making it. No token is ever printed: gh holds its own.
# Usage: tools/maintainer/post-publish.sh [--repo OWNER/NAME] [--version vX.Y.Z] [--expect-sha SHA]
#          (--denylist DIR | --skip-gate) [--export-names FILE] [--timeout MINUTES]
#          [--site DIR --site-bundle FILE [--site-repo OWNER/NAME]] [--dry-run]
# Needs gh signed in (gh auth status) as an account with admin rights on the repository, docker, curl and python3.
# Exit 0 done, 1 a step failed (its line says what to do), 2 a usage error.
set -euo pipefail
# mapfile, and an empty array expanded under set -u, need bash 4.4; macOS's own /bin/bash is 3.2
if ((BASH_VERSINFO[0] < 4 || (BASH_VERSINFO[0] == 4 && BASH_VERSINFO[1] < 4))); then
  printf 'post-publish: needs bash 4.4 or later, this is %s (on macOS: brew install bash)\n' "$BASH_VERSION" >&2
  exit 2
fi

W="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
REPO="Good-Work-Dojo/tripbox"
TAG=""
EXPECT_SHA=""
DENYLIST=""
EXPORT_NAMES=""
SITE_DIR=""
SITE_BUNDLE=""
# the maintainers' site's repository, whose pull request carries the tripbox page
SITE_REPO="Good-Work-Dojo/GoodWorkDojoLanding"
SITE_BRANCH="product/tripbox"
# every commit pushed to the site must start with this, or the push is published as a public preview
SITE_MARKER="[CF-Pages-Skip]"
# a file the base site added: where it is on main, the base site is
SITE_BASE_FILE="site/assets/analytics.js"
SKIP_GATE=0
DRY=0
TIMEOUT=60
POLL=30
RULESET="default-branch"
VARIABLE="TRIPBOX_PAGES"
# the GitHub Actions app: a required check counts only when GitHub Actions reports it
ACTIONS_APP=15368
# the page of the demo site says this once
BANNER="Demo: invented trips. Read-only."
# where the published image keeps the project's own files: the gate's generic detectors read only these
APP_PATHS=(/app)
# Where the gate counts content matches: the project's own files. Everything else is public upstream bytes the project
# did not write (the pinned Debian base, the bundled kitinerary binary, pip and uv, and the third-party packages in
# /app/.venv), whose author addresses, common words and public hosts coincide with list words and generic shapes; the
# gate prints those content matches as one count and they never fail it, while its image checks (a source map, an
# export file's name) and a damaged archive count there too. The last matching rule decides; a path is the member's as the
# layer names it, with no leading /. Own: /app, but in its virtual environment only what tripbox installs of itself.
MEMBER_RULES=(
  --upstream '*'
  --own 'app/*'
  --upstream 'app/.venv/*'
  --own 'app/.venv/lib/python*/site-packages/tripbox-*'
  --own 'app/.venv/lib/python*/site-packages/_editable_impl_tripbox.pth'
  --own 'app/.venv/bin/tripbox'
)

usage() {
  cat >&2 <<'EOF'
usage: tools/maintainer/post-publish.sh [--repo OWNER/NAME] [--version vX.Y.Z] [--expect-sha SHA]
         (--denylist DIR | --skip-gate) [--export-names FILE] [--timeout MINUTES]
         [--site DIR --site-bundle FILE [--site-repo OWNER/NAME]] [--dry-run]
EOF
  exit 2
}
refuse() { printf 'post-publish: %s\n' "$1" >&2; exit 2; }
fail() { printf 'post-publish: %s\n' "$1" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --repo) [ $# -ge 2 ] || usage; REPO="$2"; shift 2 ;;
    --version) [ $# -ge 2 ] || usage; TAG="$2"; shift 2 ;;
    --expect-sha) [ $# -ge 2 ] || usage; EXPECT_SHA="$2"; shift 2 ;;
    --denylist) [ $# -ge 2 ] || usage; DENYLIST="$2"; shift 2 ;;
    --export-names) [ $# -ge 2 ] || usage; EXPORT_NAMES="$2"; shift 2 ;;
    --timeout) [ $# -ge 2 ] || usage; TIMEOUT="$2"; shift 2 ;;
    --site) [ $# -ge 2 ] || usage; SITE_DIR="$2"; shift 2 ;;
    --site-bundle) [ $# -ge 2 ] || usage; SITE_BUNDLE="$2"; shift 2 ;;
    --site-repo) [ $# -ge 2 ] || usage; SITE_REPO="$2"; shift 2 ;;
    --skip-gate) SKIP_GATE=1; shift ;;
    --dry-run) DRY=1; shift ;;
    -h|--help) usage ;;
    *) refuse "unknown argument '$1' (--help lists them)" ;;
  esac
done

[[ "$REPO" =~ ^[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9._-]+$ ]] || refuse "--repo takes OWNER/NAME (got '$REPO')"
[[ "$SITE_REPO" =~ ^[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9._-]+$ ]] || refuse "--site-repo takes OWNER/NAME (got '$SITE_REPO')"
if { [ -n "$SITE_DIR" ] && [ -z "$SITE_BUNDLE" ]; } || { [ -z "$SITE_DIR" ] && [ -n "$SITE_BUNDLE" ]; }; then
  refuse "--site and --site-bundle go together: give both (the clone of the site repository, and the bundle of its tripbox branch)"
fi
[[ "$TIMEOUT" =~ ^[1-9][0-9]{0,3}$ ]] || refuse "--timeout takes a number of minutes (got '$TIMEOUT')"
[ -z "$EXPECT_SHA" ] || [[ "$EXPECT_SHA" =~ ^[0-9a-f]{7,40}$ ]] \
  || refuse "--expect-sha takes a commit SHA in lower-case hex, 7 to 40 characters (got '$EXPECT_SHA')"
if [ "$SKIP_GATE" = 1 ]; then
  [ -z "$DENYLIST" ] || refuse "--denylist and --skip-gate contradict each other: give one"
else
  [ -n "$DENYLIST" ] || refuse "the publish gate needs the denylist: give --denylist DIR (or --skip-gate to leave the scan out)"
  [ -d "$DENYLIST" ] || refuse "the denylist $DENYLIST is not a directory"
fi
[ -z "$EXPORT_NAMES" ] || [ -f "$EXPORT_NAMES" ] || refuse "the export names file $EXPORT_NAMES does not exist"
for tool in gh git docker curl python3; do
  command -v "$tool" >/dev/null 2>&1 || refuse "$tool is not installed: install it, then run this again"
done
OWNER="${REPO%%/*}"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
chmod 700 "$TMP"

# The JSON and text reading the steps need, in Python's standard library: tool COMMAND ARGS...
TOOL="$(cat <<'PY'
import json
import re
import sys

cmd, args = sys.argv[1], sys.argv[2:]


def load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def runs(path, event, sha):
    return [r for r in load(path).get("workflow_runs") or [] if r.get("event") == event and r.get("head_sha") == sha]


if cmd == "get":  # FILE PATH: the value at a dotted path; empty when absent, true/false for a boolean
    value = load(args[0])
    for key in args[1].split("."):
        value = value.get(key) if isinstance(value, dict) else None
    print("" if value is None else str(value).lower() if isinstance(value, bool) else value)
elif cmd == "version":  # CHANGELOG: the tag of its first section headed by a version, `## [1.2.3]`
    with open(args[0], encoding="utf-8") as f:
        for line in f:
            m = re.match(r"## \[(\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?)\]", line)
            if m:
                print("v" + m.group(1))
                break
elif cmd == "contexts":  # CI_YML: each job's check name, one a line: its `name:`, else its id
    jobs, in_jobs = [], False
    with open(args[0], encoding="utf-8") as f:
        for line in f.read().splitlines():
            if re.fullmatch(r"jobs:\s*(#.*)?", line):
                in_jobs = True
                continue
            if in_jobs and re.match(r"[^\s#]", line):
                in_jobs = False
            if not in_jobs:
                continue
            m = re.fullmatch(r"  ([A-Za-z_][A-Za-z0-9_-]*):\s*(#.*)?", line)
            if m:
                jobs.append([m.group(1), None])
                continue
            m = re.fullmatch(r"    name:\s*(.+?)\s*(#.*)?", line)
            if m and jobs and jobs[-1][1] is None:
                jobs[-1][1] = m.group(1).strip("'\"")
            if re.fullmatch(r"    strategy:.*", line):
                sys.exit("a job with a strategy reports one check per matrix entry")
    names = [name or job for job, name in jobs]
    if not names or any("${{" in n for n in names):
        sys.exit("no job, or a job named by an expression")
    print("\n".join(names))
elif cmd == "ruleset":  # NAME APP CONTEXT...: the ruleset's body
    print(json.dumps({
        "name": args[0], "target": "branch", "enforcement": "active",
        "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
        "rules": [{"type": "deletion"}, {"type": "non_fast_forward"},
                  {"type": "required_status_checks", "parameters": {
                      "strict_required_status_checks_policy": False, "do_not_enforce_on_create": False,
                      "required_status_checks": [{"context": c, "integration_id": int(args[1])} for c in args[2:]]}}],
    }))
elif cmd == "ruleset-id":  # LIST NAME: the id of the repository's own ruleset of that name; empty when none
    print(next((str(r["id"]) for r in load(args[0]) if r.get("name") == args[1]
                and r.get("source_type", "Repository") == "Repository"), ""))
elif cmd == "same-ruleset":  # WANT HAVE: exit 0 when HAVE enforces exactly what WANT says
    def norm(r):
        rules = {}
        for rule in r.get("rules") or []:
            p = rule.get("parameters") or {}
            rules[rule.get("type")] = ((bool(p.get("strict_required_status_checks_policy")), sorted(
                (c.get("context"), c.get("integration_id")) for c in p.get("required_status_checks") or []))
                if rule.get("type") == "required_status_checks" else None)
        ref = (r.get("conditions") or {}).get("ref_name") or {}
        return (r.get("target"), r.get("enforcement"), sorted(ref.get("include") or []),
                sorted(ref.get("exclude") or []), sorted(rules.items(), key=lambda kv: str(kv[0])))
    sys.exit(0 if norm(load(args[0])) == norm(load(args[1])) else 1)
elif cmd == "has-policy":  # POLICIES NAME TYPE: exit 0 when a deployment policy names that branch or tag pattern
    found = any(p.get("name") == args[1] and (p.get("type") or "branch") == args[2]
                for p in load(args[0]).get("branch_policies") or [])
    sys.exit(0 if found else 1)
elif cmd == "latest-run":  # LIST EVENT SHA: "id status conclusion url" of the newest such run; empty when none
    found = runs(*args)
    if found:
        r = max(found, key=lambda r: r.get("id") or 0)
        print(r.get("id"), r.get("status"), r.get("conclusion") or "-", r.get("html_url") or "-")
elif cmd == "release-names":  # NAMES_OUTPUT: release_names.py's `key=value` lines as image, version, rehearsal
    pairs = dict(line.split("=", 1) for line in open(args[0], encoding="utf-8").read().splitlines() if "=" in line)
    print(pairs["image"], pairs["version"], pairs["rehearsal"])
elif cmd == "platforms":  # MANIFEST: the os/architecture pairs an image index holds, one a line
    doc = load(args[0])
    print("\n".join(sorted({f"{p.get('os')}/{p.get('architecture')}" for p in
                            (m.get("platform") or {} for m in doc.get("manifests") or [])})))
elif cmd == "banner":  # HTML TEXT: exit 0 when the page holds the banner's text
    with open(args[0], encoding="utf-8", errors="replace") as f:
        sys.exit(0 if args[1] in f.read() else 1)
elif cmd == "csp":  # HTML: exit 0 when the page carries a content-security policy <meta> tag
    with open(args[0], encoding="utf-8", errors="replace") as f:
        page = f.read()
    sys.exit(0 if re.search(r"<meta\b[^>]*http-equiv\s*=\s*[\"']?content-security-policy", page, re.I) else 1)
else:
    sys.exit(f"unknown command {cmd}")
PY
)"
tool() { python3 -I -c "$TOOL" "$@"; }

show() { printf '+'; printf ' %q' "$@"; printf '\n'; }
step() { printf '\n== %s/8 %s ==\n' "$1" "$2"; }

# get ENDPOINT FILE: a read, its body in FILE. Returns 0, or 4 when the resource does not exist (HTTP 404); any other
# failure stops the script.
get() {
  show gh api -X GET "$1"
  if gh api -X GET "$1" >"$2" 2>"$TMP/err"; then return 0; fi
  if grep -q 'HTTP 404' "$TMP/err"; then return 4; fi
  fail "reading $1 failed ($(tail -n 1 "$TMP/err")): check that gh auth status shows an account with admin rights on $REPO, then run this again"
}

# change COMMAND...: a call that changes the repository: printed, and made unless this is a dry run.
change() {
  if [ "$DRY" = 1 ]; then printf 'would run:'; printf ' %q' "$@"; printf '\n'; return 0; fi
  show "$@"
  "$@" >"$TMP/out" 2>"$TMP/err" \
    || fail "$(tail -n 1 "$TMP/err"): fix what it says, then run this again (the steps already done are skipped)"
}

# change_json METHOD ENDPOINT JSON: a change whose request body is JSON.
change_json() {
  printf '%s\n' "$3" >"$TMP/body.json"
  if [ "$DRY" = 1 ]; then printf '  with body: %s\n' "$3"; fi
  change gh api -X "$1" "$2" --input "$TMP/body.json"
}

# wait_run WORKFLOW EVENT [AFTER]: waits, up to --timeout minutes, for the newest run of WORKFLOW on the head from
# EVENT to finish, and with AFTER for one newer than that run id (a run just asked for, which GitHub may not list yet);
# anything but success stops the script.
wait_run() {
  local deadline=$((SECONDS + TIMEOUT * 60)) run="" id status conclusion url="" after="${3:-}"
  while :; do
    get "repos/$REPO/actions/workflows/$1/runs?event=$2&head_sha=$HEAD&per_page=20" "$TMP/runs.json" \
      || fail "the workflow $1 is not in $REPO: publish the tree that holds it, then run this again"
    run="$(tool latest-run "$TMP/runs.json" "$2" "$HEAD")"
    if [ -n "$run" ]; then
      read -r id status conclusion url <<<"$run"
    fi
    if [ -n "$run" ] && { [ -z "$after" ] || [ "$id" -gt "$after" ]; }; then
      if [ "$status" = completed ]; then
        [ "$conclusion" = success ] \
          || fail "$1's run ended '$conclusion' ($url): read its log there, re-run it once the cause is fixed, then run this again"
        echo "$1: run $id succeeded ($url)"
        return 0
      fi
    fi
    [ "$SECONDS" -lt "$deadline" ] \
      || fail "$1's run on $HEAD did not finish within $TIMEOUT minutes${url:+ ($url)}: wait for it (or pass a longer --timeout), then run this again"
    sleep "$POLL"
  done
}

if [ "$DRY" = 1 ]; then echo "dry run: reads are made, and every change is printed instead of made"; fi

step 1 "the repository is public at the expected commit"
show gh auth status
gh auth status >/dev/null 2>&1 \
  || fail "gh is not signed in: run gh auth login as an account with admin rights on $REPO, then run this again"
get "repos/$REPO" "$TMP/repo.json" \
  || fail "$REPO does not exist or this account cannot see it: check --repo and gh auth status, then run this again"
VISIBILITY="$(tool get "$TMP/repo.json" visibility)"
[ "$VISIBILITY" = public ] \
  || fail "$REPO is ${VISIBILITY:-not public}; make it public first (nothing was changed)"
[ "$(tool get "$TMP/repo.json" permissions.admin)" = true ] \
  || fail "the account gh is signed in as has no admin rights on $REPO: sign in as an owner (gh auth login), then run this again"
# GitHub follows a renamed or moved repository on a read; the script acts on the one named, never a fork
FULL_NAME="$(tool get "$TMP/repo.json" full_name)"
[ "${FULL_NAME,,}" = "${REPO,,}" ] \
  || fail "$REPO is now ${FULL_NAME:-another name} on GitHub: give that as --repo, then run this again (nothing was changed)"
[ "$(tool get "$TMP/repo.json" fork)" != true ] \
  || fail "$REPO is a fork: run this on the project's own repository (nothing was changed)"
OWNER_TYPE="$(tool get "$TMP/repo.json" owner.type)"
BRANCH="$(tool get "$TMP/repo.json" default_branch)"
# the branch's name goes into paths and request bodies: a ref name, nothing else
[[ "$BRANCH" =~ ^[A-Za-z0-9._/-]{1,200}$ && "$BRANCH" != *..* ]] \
  || fail "$REPO's default branch name could not be read as a branch name: check it, then run this again (nothing was changed)"
get "repos/$REPO/branches/$BRANCH" "$TMP/branch.json" || fail "$REPO has no branch $BRANCH: push the tree, then run this again"
HEAD="$(tool get "$TMP/branch.json" commit.sha)"
[[ "$HEAD" =~ ^[0-9a-f]{40}$ ]] || fail "the head of $BRANCH could not be read: run this again"
if [ -n "$EXPECT_SHA" ]; then
  case "$HEAD" in
    "$EXPECT_SHA"*) echo "$REPO is public; the head of $BRANCH is $HEAD, as expected" ;;
    *) fail "the head of $BRANCH is $HEAD, not $EXPECT_SHA: check what was pushed; nothing was changed" ;;
  esac
else
  echo "$REPO is public; the head of $BRANCH is $HEAD"
fi
# the version, the notes and the job names below are read from this checkout, so it must be that commit, untouched
show git -C "$W" rev-parse HEAD
LOCAL="$(git -C "$W" rev-parse HEAD 2>/dev/null)" \
  || fail "this directory is not a git checkout: run the script from a clone of $REPO (nothing was changed)"
[ "$LOCAL" = "$HEAD" ] \
  || fail "this checkout is at $LOCAL, not the head of $BRANCH ($HEAD): check out that commit (git pull), then run this again (nothing was changed)"
show git -C "$W" status --porcelain
DIRTY="$(git -C "$W" status --porcelain 2>/dev/null)" \
  || fail "the checkout's state could not be read: run this again (nothing was changed)"
[ -z "$DIRTY" ] || fail "this checkout has uncommitted changes: commit or discard them, then run this again (nothing was changed)"

# every check that needs no change comes first: the version, its notes, the required checks, the tag
if [ -z "$TAG" ]; then
  TAG="$(tool version "$W/CHANGELOG.md")" || true
  [ -n "$TAG" ] || fail "CHANGELOG.md has no section headed by a version: give --version vX.Y.Z (nothing was changed)"
fi
case "$TAG" in v*) ;; *) TAG="v$TAG" ;; esac
python3 -I "$W/tools/verify/release_names.py" names "$REPO" "$TAG" >"$TMP/names" \
  || fail "the version $TAG gives no release names (the line above): give --version vX.Y.Z (nothing was changed)"
read -r IMAGE IMAGE_VERSION REHEARSAL <<<"$(tool release-names "$TMP/names")"
[ "$REHEARSAL" != true ] \
  || fail "$TAG is a prerelease: release candidates are proven by release.yml alone, which removes its own images; give the full version (nothing was changed)"
python3 -I "$W/tools/verify/release_names.py" matches "$W/pyproject.toml" "$TAG" \
  || fail "$TAG is not the version in pyproject.toml (the line above): fix one of them, then run this again (nothing was changed)"
python3 -I "$W/tools/verify/release_names.py" notes "$W/CHANGELOG.md" "$TAG" >"$TMP/notes.md" \
  || fail "CHANGELOG.md has no notes for $TAG (the line above): add its section, then run this again (nothing was changed)"
mapfile -t CONTEXTS < <(tool contexts "$W/.github/workflows/ci.yml")
[ "${#CONTEXTS[@]}" -gt 0 ] \
  || fail "the job of .github/workflows/ci.yml could not be read: run this from a checkout of $REPO (nothing was changed)"
WANT="$(tool ruleset "$RULESET" "$ACTIONS_APP" "${CONTEXTS[@]}")"
printf '%s\n' "$WANT" >"$TMP/want.json"
# an existing release, or a tag with no release, must already point at this commit: GitHub publishes a release at
# an existing tag wherever the tag is, whatever --target says
RELEASED=0
rc=0; get "repos/$REPO/releases/tags/$TAG" "$TMP/release.json" || rc=$?
if [ "$rc" = 0 ]; then
  RELEASED=1
else
  rc=0; get "repos/$REPO/git/ref/tags/$TAG" "$TMP/tagref.json" || rc=$?
fi
if [ "$RELEASED" = 1 ] && [ "$(tool get "$TMP/release.json" prerelease)" = true ]; then
  fail "the release $TAG is marked a prerelease on GitHub: mark it a full release (or delete it), then run this again (nothing was changed)"
fi
if [ "$RELEASED" = 1 ] || [ "$rc" = 0 ]; then
  get "repos/$REPO/commits/$TAG" "$TMP/tag.json" || fail "the tag $TAG could not be read: run this again (nothing was changed)"
  [ "$(tool get "$TMP/tag.json" sha)" = "$HEAD" ] \
    || fail "the tag $TAG already exists on another commit than the head $HEAD: delete it (and any release of it), then run this again (nothing was changed)"
fi
# the full version is the tag every release pushes: a patch to an older line moves no major tag
PULL_TAG="$IMAGE_VERSION"
echo "release $TAG: $IMAGE:$PULL_TAG"

# the site step's checks, all made here: the clone, its remote and state, the base site, the bundle and its tip
MERGED_PR=""
SITE_MOVED=0
SITE_BEHIND=0
SITE_REMOTE=""
sgit() { git -C "$SITE_DIR" "$@"; }
if [ -n "$SITE_DIR" ]; then
  [ -d "$SITE_DIR" ] || fail "--site $SITE_DIR is not a directory: give a clone of $SITE_REPO (nothing was changed)"
  SITE_DIR="$(cd "$SITE_DIR" && pwd -P)"
  sgit rev-parse --git-dir >/dev/null 2>&1 \
    || fail "--site $SITE_DIR is not a git clone: give a clone of $SITE_REPO (nothing was changed)"
  # every address the clone fetches from and pushes to (a push URL may differ) must be the site's repository
  for kind in "" "--push"; do
    # shellcheck disable=SC2086
    SITE_URLS="$(sgit remote get-url $kind --all origin 2>/dev/null)" \
      || fail "the clone $SITE_DIR has no remote named origin: give a clone of $SITE_REPO (nothing was changed)"
    [ -n "$SITE_URLS" ] || fail "the origin of $SITE_DIR has no address: give a clone of $SITE_REPO (nothing was changed)"
    while IFS= read -r SITE_URL; do
      SITE_PATH="${SITE_URL,,}"
      SITE_PATH="${SITE_PATH%/}"
      SITE_PATH="${SITE_PATH%.git}"
      SITE_PATH="${SITE_PATH#https://github.com/}"
      SITE_PATH="${SITE_PATH#ssh://git@github.com/}"
      SITE_PATH="${SITE_PATH#git@github.com:}"
      [ "$SITE_PATH" = "${SITE_REPO,,}" ] \
        || fail "an address of origin in $SITE_DIR${kind:+ (a push address)} is not $SITE_REPO: give a clone whose origin fetches from and pushes to the site's repository only (nothing was changed)"
    done <<<"$SITE_URLS"
  done
  [ -z "$(sgit status --porcelain 2>/dev/null)" ] \
    || fail "the site clone $SITE_DIR has uncommitted changes: commit or discard them, then run this again (nothing was changed)"
  [ -f "$SITE_DIR/tools/check_site.py" ] \
    || fail "the site clone $SITE_DIR has no tools/check_site.py: give a clone of $SITE_REPO (nothing was changed)"
  [ -f "$SITE_BUNDLE" ] || fail "--site-bundle $SITE_BUNDLE is not a file (nothing was changed)"
  SITE_BUNDLE="$(cd "$(dirname "$SITE_BUNDLE")" && pwd -P)/$(basename "$SITE_BUNDLE")"
  show gh pr list --repo "$SITE_REPO" --head "$SITE_BRANCH" --base main --state merged
  MERGED_PR="$(gh pr list --repo "$SITE_REPO" --head "$SITE_BRANCH" --base main --state merged --json url --jq '.[0].url // empty' 2>"$TMP/err")" \
    || fail "the pull requests of $SITE_REPO could not be read ($(tail -n 1 "$TMP/err")): run this again (nothing was changed)"
  if [ -n "$MERGED_PR" ]; then
    # a merged pull request for the branch is the page already live: there is nothing to push or open
    echo "the pull request for $SITE_BRANCH is already merged ($MERGED_PR): the page is live; step 8 has nothing to do"
  else
    # what is checked here only reads, or changes the clone's own refs, never anything public
    show git -C "$SITE_DIR" fetch origin
    sgit fetch origin >/dev/null 2>"$TMP/err" \
      || fail "git fetch origin failed in $SITE_DIR ($(tail -n 1 "$TMP/err")): check the network, then run this again (nothing was changed)"
    sgit cat-file -e "origin/main:$SITE_BASE_FILE" 2>/dev/null \
      || fail "the base site is not on main; tell the owner (origin/main in $SITE_DIR lacks $SITE_BASE_FILE; nothing was changed)"
    sgit bundle verify "$SITE_BUNDLE" >/dev/null 2>"$TMP/err" \
      || fail "$SITE_BUNDLE is not a valid bundle for the clone ($(tail -n 1 "$TMP/err")): give the bundle of the branch cut from the site's $SITE_BRANCH (nothing was changed)"
    [ -n "$(sgit bundle list-heads "$SITE_BUNDLE" "refs/heads/$SITE_BRANCH" 2>/dev/null)" ] \
      || fail "$SITE_BUNDLE holds no $SITE_BRANCH: give the bundle of that branch (nothing was changed)"
    sgit update-ref -d refs/tripbox-bundle/tip >/dev/null 2>&1 || true
    sgit fetch "$SITE_BUNDLE" "$SITE_BRANCH:refs/tripbox-bundle/tip" >/dev/null 2>"$TMP/err" \
      || fail "the bundle could not be read into $SITE_DIR ($(tail -n 1 "$TMP/err")): run this again (nothing was changed)"
    SITE_OURS=refs/tripbox-bundle/tip
    SITE_BEHIND=0
    if sgit rev-parse --verify -q "refs/heads/$SITE_BRANCH" >/dev/null 2>&1; then
      if sgit merge-base --is-ancestor refs/tripbox-bundle/tip "refs/heads/$SITE_BRANCH"; then
        SITE_OURS="refs/heads/$SITE_BRANCH"   # at the bundle's tip, or ahead of it with work of its own
      elif sgit merge-base --is-ancestor "refs/heads/$SITE_BRANCH" refs/tripbox-bundle/tip; then
        SITE_BEHIND=1                         # an older copy of the branch: step 8 fast-forwards it
      else
        fail "the local $SITE_BRANCH in $SITE_DIR has commits the bundle lacks (see git log refs/tripbox-bundle/tip..$SITE_BRANCH there): put it aside (git branch -m $SITE_BRANCH $SITE_BRANCH-old) or give a fresh clone, then run this again (nothing was changed)"
      fi
    fi
    # the tip that would be pushed must start with the marker, or the push is published as a preview
    SUBJECT="$(sgit log -1 --format=%s "$SITE_OURS")" \
      || fail "the tip's message of $SITE_BRANCH could not be read: run this again (nothing was changed)"
    case "$SUBJECT" in
      "$SITE_MARKER"*) ;;
      *) fail "the tip of $SITE_BRANCH does not start with $SITE_MARKER, so pushing it would publish a preview: add a commit that starts with it, never rewrite the branch, then run this again (nothing was changed)" ;;
    esac
    # a moved remote is merged in at step 8: it must merge without conflict
    SITE_REMOTE="$(sgit rev-parse --verify -q "refs/remotes/origin/$SITE_BRANCH" 2>/dev/null)" || SITE_REMOTE=""
    SITE_MOVED=0
    if [ -n "$SITE_REMOTE" ] && ! sgit merge-base --is-ancestor "refs/remotes/origin/$SITE_BRANCH" "$SITE_OURS"; then
      SITE_MOVED=1
      rc=0; sgit merge-tree --write-tree "$SITE_OURS" "refs/remotes/origin/$SITE_BRANCH" >/dev/null 2>&1 || rc=$?
      [ "$rc" != 1 ] \
        || fail "origin/$SITE_BRANCH moved and would not merge into the bundle's branch without a conflict: tell the owner (nothing was changed)"
      [ "$rc" = 0 ] \
        || fail "git merge-tree could not test the merge of origin/$SITE_BRANCH (it needs git 2.38 or later): update git, then run this again (nothing was changed)"
    fi
    SITE_NOTE=""
    if [ "$SITE_MOVED" = 1 ]; then SITE_NOTE="; origin/$SITE_BRANCH moved and merges cleanly"; fi
    echo "site clone $SITE_DIR: origin is $SITE_REPO and clean, the base site is on main, the bundle's tip starts $SITE_MARKER$SITE_NOTE"
  fi
fi

step 2 "security features"
if get "repos/$REPO/vulnerability-alerts" "$TMP/alerts"; then
  echo "Dependabot alerts: already on"
else
  change gh api -X PUT "repos/$REPO/vulnerability-alerts"
fi
rc=0; get "repos/$REPO/automated-security-fixes" "$TMP/fixes.json" || rc=$?
if [ "$rc" = 0 ] && [ "$(tool get "$TMP/fixes.json" enabled)" = true ]; then
  echo "Dependabot security updates: already on"
else
  change gh api -X PUT "repos/$REPO/automated-security-fixes"
fi
if [ "$(tool get "$TMP/repo.json" security_and_analysis.secret_scanning.status)" = enabled ] \
    && [ "$(tool get "$TMP/repo.json" security_and_analysis.secret_scanning_push_protection.status)" = enabled ]; then
  echo "secret scanning and push protection: already on"
else
  change_json PATCH "repos/$REPO" \
    '{"security_and_analysis":{"secret_scanning":{"status":"enabled"},"secret_scanning_push_protection":{"status":"enabled"}}}'
fi
rc=0; get "repos/$REPO/private-vulnerability-reporting" "$TMP/pvr.json" || rc=$?
if [ "$rc" = 0 ] && [ "$(tool get "$TMP/pvr.json" enabled)" = true ]; then
  echo "private vulnerability reporting: already on"
else
  change gh api -X PUT "repos/$REPO/private-vulnerability-reporting"
fi
rc=0; get "repos/$REPO/code-scanning/default-setup" "$TMP/codeql.json" || rc=$?
if [ "$rc" = 0 ] && [ "$(tool get "$TMP/codeql.json" state)" = configured ]; then
  echo "CodeQL default setup: already configured"
else
  change_json PATCH "repos/$REPO/code-scanning/default-setup" '{"state":"configured"}'
  echo "CodeQL's first analysis runs in the background"
fi

step 3 "the default branch's ruleset"
echo "required checks (from ci.yml): ${CONTEXTS[*]}"
get "repos/$REPO/rulesets?includes_parents=false&per_page=100" "$TMP/rulesets.json" \
  || fail "the rulesets of $REPO could not be read: check the account's admin rights, then run this again"
RULESET_ID="$(tool ruleset-id "$TMP/rulesets.json" "$RULESET")"
if [ -z "$RULESET_ID" ]; then
  change_json POST "repos/$REPO/rulesets" "$WANT"
else
  get "repos/$REPO/rulesets/$RULESET_ID" "$TMP/have.json" || fail "the ruleset $RULESET vanished while read: run this again"
  if tool same-ruleset "$TMP/want.json" "$TMP/have.json"; then
    echo "ruleset $RULESET: already as wanted"
  else
    change_json PUT "repos/$REPO/rulesets/$RULESET_ID" "$WANT"
  fi
fi

step 4 "the demo site's switches"
rc=0; get "repos/$REPO/actions/variables/$VARIABLE" "$TMP/variable.json" || rc=$?
if [ "$rc" = 4 ]; then
  change_json POST "repos/$REPO/actions/variables" "{\"name\":\"$VARIABLE\",\"value\":\"true\"}"
elif [ "$(tool get "$TMP/variable.json" value)" != true ]; then
  change_json PATCH "repos/$REPO/actions/variables/$VARIABLE" "{\"name\":\"$VARIABLE\",\"value\":\"true\"}"
else
  echo "$VARIABLE: already true"
fi
rc=0; get "repos/$REPO/pages" "$TMP/pages.json" || rc=$?
if [ "$rc" = 4 ]; then
  change_json POST "repos/$REPO/pages" '{"build_type":"workflow"}'
elif [ "$(tool get "$TMP/pages.json" build_type)" != workflow ]; then
  change_json PUT "repos/$REPO/pages" '{"build_type":"workflow"}'
else
  echo "Pages: already built by Actions"
fi
# pages.yml also runs on a published release, from its tag: the github-pages environment must let v* tags deploy as
# well as the default branch (an environment that lets every ref deploy needs nothing)
ENVIRONMENT="repos/$REPO/environments/github-pages"
rc=0; get "$ENVIRONMENT" "$TMP/environment.json" || rc=$?
if [ "$rc" = 0 ] && [ -z "$(tool get "$TMP/environment.json" deployment_branch_policy)" ]; then
  echo "github-pages environment: every branch and tag may deploy already"
else
  if [ "$rc" = 0 ] && [ "$(tool get "$TMP/environment.json" deployment_branch_policy.custom_branch_policies)" = true ]; then
    get "$ENVIRONMENT/deployment-branch-policies?per_page=100" "$TMP/policies.json" \
      || fail "the github-pages environment's deployment policies could not be read: run this again"
  else
    # absent, or protected branches only: switch to chosen branches and tags, then name the default branch too
    change_json PUT "$ENVIRONMENT" '{"deployment_branch_policy":{"protected_branches":false,"custom_branch_policies":true}}'
    echo '{"branch_policies":[]}' >"$TMP/policies.json"
  fi
  for policy in "$BRANCH branch" "v* tag"; do
    read -r name type <<<"$policy"
    if tool has-policy "$TMP/policies.json" "$name" "$type"; then
      echo "github-pages environment: the $type $name may deploy already"
    else
      change_json POST "$ENVIRONMENT/deployment-branch-policies" "{\"name\":\"$name\",\"type\":\"$type\"}"
    fi
  done
fi
get "repos/$REPO/actions/workflows/pages.yml/runs?event=workflow_dispatch&head_sha=$HEAD&per_page=20" "$TMP/pages-runs.json" \
  || fail "the workflow pages.yml is not in $REPO: publish the tree that holds it, then run this again"
read -r PAGES_ID PAGES_STATUS PAGES_CONCLUSION _ <<<"$(tool latest-run "$TMP/pages-runs.json" workflow_dispatch "$HEAD") - - - -"
PAGES_AFTER=""  # the run step 7 waits for must be newer than this one, when one is asked for here
if [ "$PAGES_CONCLUSION" = success ] || { [ "$PAGES_STATUS" != - ] && [ "$PAGES_STATUS" != completed ]; }; then
  echo "pages.yml: a run on $HEAD has succeeded or is under way"
else
  change_json POST "repos/$REPO/actions/workflows/pages.yml/dispatches" "{\"ref\":\"$BRANCH\"}"
  if [[ "$PAGES_ID" =~ ^[0-9]+$ ]]; then PAGES_AFTER="$PAGES_ID"; else PAGES_AFTER=0; fi
fi

step 5 "the release $TAG"
if [ "$RELEASED" = 1 ]; then
  echo "release $TAG: already published at $HEAD"
else
  echo "notes: CHANGELOG.md's section for $TAG ($(grep -c '' "$TMP/notes.md") lines)"
  change gh release create "$TAG" --repo "$REPO" --target "$HEAD" --title "$TAG" --notes-file "$TMP/notes.md"
  [ "$DRY" = 1 ] || RELEASED=1
fi
if [ "$RELEASED" = 1 ]; then
  wait_run release.yml release
else
  echo "would wait for release.yml's run on $HEAD"
fi

step 6 "the image is public and holds nothing from the denylist"
IMAGES=("$IMAGE:$PULL_TAG")
# an empty docker configuration holds no sign-in and no credential helper, so this pull is anonymous
ANON="$TMP/anonymous-docker"
mkdir -m 700 "$ANON"
package_link() {
  local kind=orgs
  [ "$OWNER_TYPE" = Organization ] || kind=users
  echo "  https://github.com/$kind/$OWNER/packages/container/${IMAGE#ghcr.io/*/}/settings"
}
# The empty configuration also drops the active docker context (Colima, Rancher, some Docker Desktop setups), so the
# daemon that context names is kept by its address.
DAEMON="${DOCKER_HOST:-}"
if [ -z "$DAEMON" ] && [ "$DRY" = 0 ]; then
  DAEMON="$(docker context inspect --format '{{.Endpoints.docker.Host}}' 2>/dev/null || true)"
fi
anonymous() {
  if [ -n "$DAEMON" ]; then
    env DOCKER_CONFIG="$ANON" DOCKER_HOST="$DAEMON" "$@"
  else
    env DOCKER_CONFIG="$ANON" "$@"
  fi
}
for image in "${IMAGES[@]}"; do
  if [ "$DRY" = 1 ]; then
    printf 'would run: DOCKER_CONFIG=<an empty directory> docker pull -q %s\n' "$image"
    printf 'would run: DOCKER_CONFIG=<an empty directory> docker manifest inspect %s\n' "$image"
    continue
  fi
  show env DOCKER_CONFIG="$ANON" docker pull -q "$image"
  show env DOCKER_CONFIG="$ANON" docker manifest inspect "$image"
  if ! anonymous docker pull -q "$image" >/dev/null 2>"$TMP/err" \
      || ! anonymous docker manifest inspect "$image" >"$TMP/manifest.json" 2>>"$TMP/err"; then
    said="$(tail -n 1 "$TMP/err")"
    # only a refusal of the registry says the package is private; anything else is the daemon or the network
    if ! grep -Eqi 'denied|unauthorized|authentication required|forbidden|40[13]' "$TMP/err"; then
      fail "$image could not be pulled ($said): check that docker runs and reaches ghcr.io, then run this again"
    fi
    printf 'post-publish: %s could not be pulled without signing in, so its package is not public yet (%s).\n' \
      "$image" "$said" >&2
    echo "Open the package's settings, set its visibility to Public (Danger zone), then run this again:" >&2
    package_link >&2
    exit 1
  fi
  PLATFORMS="$(tool platforms "$TMP/manifest.json")"
  for platform in linux/amd64 linux/arm64; do
    grep -qx "$platform" <<<"$PLATFORMS" \
      || fail "$image holds no $platform image: re-run release.yml for $TAG (Actions, release), then run this again"
  done
  echo "$image: pulled anonymously, linux/amd64 and linux/arm64"
done
if [ "$SKIP_GATE" = 1 ]; then
  echo "publish gate: skipped (--skip-gate)"
else
  # the image carries no list of export files: the scan's own checks still run with an empty one
  if [ -z "$EXPORT_NAMES" ]; then
    EXPORT_NAMES="$TMP/export-names"
    : >"$EXPORT_NAMES"
  fi
  GATE=(python3 -I "$W/tools/publish_gate.py" images --denylist "$DENYLIST" --export-names "$EXPORT_NAMES")
  for prefix in "${APP_PATHS[@]}"; do GATE+=(--app-path "$prefix"); done
  GATE+=("${MEMBER_RULES[@]}")
  GATE+=("${IMAGES[@]}")
  if [ "$DRY" = 1 ]; then
    printf 'would run:'; printf ' %q' "${GATE[@]}"; printf '\n'
  else
    show "${GATE[@]}"
    rc=0; "${GATE[@]}" || rc=$?
    case "$rc" in
      0) ;;
      1) printf 'post-publish: the publish gate found something in the published image (its lines above): make the package private at once, then find its source before publishing again:\n' >&2
         package_link >&2
         exit 1 ;;
      *) fail "the publish gate could not scan the image (its error above): fix it, then run this again" ;;
    esac
  fi
fi

step 7 "the demo site answers"
if [ "$DRY" = 1 ]; then
  echo "would wait for pages.yml's run on $HEAD, then fetch the site's page"
else
  wait_run pages.yml workflow_dispatch "$PAGES_AFTER"
  get "repos/$REPO/pages" "$TMP/pages.json" || fail "Pages is not on for $REPO: run this again"
  SITE="$(tool get "$TMP/pages.json" html_url)"
  [ -n "$SITE" ] || fail "Pages has no address for $REPO yet: run this again in a few minutes"
  SITE="${SITE%/}/"
  PAGE_CODE=000
  for attempt in 1 2 3 4 5; do
    [ "$attempt" = 1 ] || sleep 15
    show curl -sS -L --max-time 30 -o "$TMP/page.html" -w '%{http_code}' "$SITE"
    PAGE_CODE="$(curl -sS -L --max-time 30 -o "$TMP/page.html" -w '%{http_code}' "$SITE" 2>/dev/null)" || PAGE_CODE=000
    [ "$PAGE_CODE" != 200 ] || break
  done
  [ "$PAGE_CODE" = 200 ] \
    || fail "the demo site at $SITE answered $PAGE_CODE: check pages.yml's last run, then run this again"
  tool banner "$TMP/page.html" "$BANNER" \
    || fail "the demo site's page does not hold its banner: check the demo build (tripbox demo export), then run this again"
  tool csp "$TMP/page.html" \
    || fail "the demo site's page carries no content-security policy <meta> tag: check the demo build (tripbox demo export), then run this again"
  echo "demo site: $SITE answers 200, holds its banner and carries its content-security policy"
fi

step 8 "the site's tripbox page"
if [ -z "$SITE_DIR" ]; then
  echo "no --site given: the site's page is left for you. In a clone of $SITE_REPO, with the page's bundle:"
  echo "  git fetch origin"
  echo "  git cat-file -e origin/main:$SITE_BASE_FILE    # the base site must be on main; if not, stop"
  echo "  git fetch BUNDLE $SITE_BRANCH:refs/tripbox-bundle/tip, then check out $SITE_BRANCH at it"
  echo "  git merge --no-ff -m \"$SITE_MARKER Merge origin/$SITE_BRANCH\" origin/$SITE_BRANCH    # only if the remote moved; never rebase"
  echo "  the tip's message must start $SITE_MARKER; python3 tools/check_site.py --no-provisional"
  echo "  git push origin $SITE_BRANCH:$SITE_BRANCH    # never forced"
  echo "  gh pr create --repo $SITE_REPO --base main --head $SITE_BRANCH"
  echo "Merging that pull request is going live: yours to do, as a merge commit whose message does not start with $SITE_MARKER."
else
  PUSH=(git -C "$SITE_DIR" push origin "$SITE_BRANCH:$SITE_BRANCH")
  if [ -n "$MERGED_PR" ]; then
    echo "nothing to do: $SITE_BRANCH was merged ($MERGED_PR)"
  elif [ "$DRY" = 1 ]; then
    echo "would make $SITE_BRANCH in $SITE_DIR from the bundle (step 1 fetched it), merge origin/$SITE_BRANCH into it if it moved, and run python3 tools/check_site.py --no-provisional"
    printf 'would run:'; printf ' %q' "${PUSH[@]}"; printf '\n'
  else
    if sgit rev-parse --verify -q "refs/heads/$SITE_BRANCH" >/dev/null 2>&1; then
      show git -C "$SITE_DIR" checkout "$SITE_BRANCH"
      sgit checkout "$SITE_BRANCH" >/dev/null 2>"$TMP/err" \
        || fail "$SITE_BRANCH could not be checked out in $SITE_DIR ($(tail -n 1 "$TMP/err")): run this again (nothing was pushed)"
      if [ "$SITE_BEHIND" = 1 ]; then
        show git -C "$SITE_DIR" merge --ff-only refs/tripbox-bundle/tip
        sgit merge --ff-only refs/tripbox-bundle/tip >/dev/null 2>"$TMP/err" \
          || fail "$SITE_BRANCH could not be fast-forwarded to the bundle ($(tail -n 1 "$TMP/err")): run this again (nothing was pushed)"
      fi
    else
      show git -C "$SITE_DIR" checkout -b "$SITE_BRANCH" refs/tripbox-bundle/tip
      sgit checkout -b "$SITE_BRANCH" refs/tripbox-bundle/tip >/dev/null 2>"$TMP/err" \
        || fail "$SITE_BRANCH could not be made in $SITE_DIR ($(tail -n 1 "$TMP/err")): run this again (nothing was pushed)"
    fi
    # when the remote moved since the bundle was cut, its work is merged in with a marked commit, never forced
    if [ "$SITE_MOVED" = 1 ]; then
      SITE_MERGE="$SITE_MARKER Merge origin/$SITE_BRANCH"
      show git -C "$SITE_DIR" merge --no-ff -m "$SITE_MERGE" "refs/remotes/origin/$SITE_BRANCH"
      if ! sgit merge --no-ff -m "$SITE_MERGE" "refs/remotes/origin/$SITE_BRANCH" >/dev/null 2>"$TMP/err"; then
        sgit merge --abort >/dev/null 2>&1 || true
        fail "origin/$SITE_BRANCH could not be merged into $SITE_BRANCH ($(tail -n 1 "$TMP/err")): tell the owner; nothing was pushed"
      fi
    fi
    SITE_TIP="$(sgit rev-parse "refs/heads/$SITE_BRANCH")" \
      || fail "the tip of $SITE_BRANCH could not be read in $SITE_DIR: run this again (nothing was pushed)"
    show python3 tools/check_site.py --no-provisional
    (cd "$SITE_DIR" && python3 tools/check_site.py --no-provisional) \
      || fail "python3 tools/check_site.py --no-provisional failed in $SITE_DIR (its lines above): fix the page, then run this again (nothing was pushed)"
    if [ "$SITE_REMOTE" = "$SITE_TIP" ]; then
      echo "$SITE_BRANCH: already pushed"
    else
      show "${PUSH[@]}"
      "${PUSH[@]}" >/dev/null 2>"$TMP/err" \
        || fail "the push was refused ($(tail -n 1 "$TMP/err")): never force it; look at origin/$SITE_BRANCH, then run this again"
    fi
  fi
  if [ -z "$MERGED_PR" ]; then
    show gh pr list --repo "$SITE_REPO" --head "$SITE_BRANCH" --base main --state open
    OPEN_PR="$(gh pr list --repo "$SITE_REPO" --head "$SITE_BRANCH" --base main --state open --json url --jq '.[0].url // empty' 2>"$TMP/err")" \
      || fail "the open pull requests of $SITE_REPO could not be read ($(tail -n 1 "$TMP/err")): run this again"
    if [ -n "$OPEN_PR" ]; then
      echo "the pull request is already open: $OPEN_PR"
    else
      change gh pr create --repo "$SITE_REPO" --base main --head "$SITE_BRANCH" \
        --title "Add the tripbox page" \
        --body "The tripbox page, from the tripbox project. Merging this pull request puts it live at once: use a merge commit whose message does not start with $SITE_MARKER, then run the live check."
      [ "$DRY" = 1 ] || echo "pull request opened: $(tail -n 1 "$TMP/out")"
    fi
    echo "merging it is going live, and yours to do: a merge commit whose message does not start with $SITE_MARKER, then the site's live check"
  fi
fi

if [ "$DRY" = 1 ]; then echo; echo "dry run: nothing was changed"; else echo; echo "done: $REPO is published as $TAG"; fi
# The one step no API call makes: the contributor agreement's app is installed by hand, once.
echo "by hand, once: install the CLA Assistant GitHub App on $REPO, pointed at a Gist holding CLA.md, so that it asks the author of each first pull request to sign"
