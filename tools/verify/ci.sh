#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
# The verification gate: every check runs in Docker, so the host needs bash, git, tar and Docker
# and no Python (Linux or macOS; the lock is flock(1)'s, else perl's, else none). Stages, in order,
# each printed as `== <name> ==` and each stopping the run when it fails;
# .github/workflows/ci.yml runs the same stages under the same names (then the stack and the
# browser, which only a workflow starts):
#   AGENTS.md byte budget   AGENTS.md is at most 40000 bytes
#   workflow rules          check_workflows.py, in the Dockerfile's own Python image (PYTHON) with
#                           the repository mounted read-only
#   licence headers         spdx.py: every source file carries the licence header (the same way)
#   test image              build tripboxoss:test (the repository's Dockerfile, target test); its
#                           image id is recorded and read again after the last stage
#   dependency licences     licences.py: the runtime target built as tripboxoss:rt (the test
#                           image's own first stage, already built), `licences.py --collect` and
#                           `licences.py --dists` run in it with the repository mounted read-only
#                           and no network: every package's licence is on the allow-list and the
#                           notices' block is current
#   lint, tests             ruff check src tests, then pytest -q (in the image as built; pytest as
#                           uid 12345:54321, which no account names, with no bytecode or cache
#                           written)
#   a new install as any user  standalone_smoke.py as that same user, on a read-only root with
#                           /tmp a tmpfs and the repository mounted read-only at /repo ->
#                           `standalone smoke: ok`
#   corpus scorecard        `tripbox eval --recorded` as that same user on a read-only root ->
#                           `eval: N of N emails right`
#   tools lint, tools tests ruff check tools, then pytest -q tools (the repository mounted
#                           read-only at /repo, with TRIPBOXOSS_STRICT=1, so a test of the docker
#                           wrapper scripts that its environment would skip fails instead)
#   demo site               `tripbox demo export` into a tmpfs in the image as that same user, then
#                           tools/verify/demo_site_check.py on it -> `demo site: ok <n> pages`
#   secrets                 gitleaks (its image pulled by digest), with no network, over a copy of
#                           (world-readable, since the image runs as a user of its own)
#                           what git would commit (the tracked files as they stand and the new
#                           ones it does not ignore; never an ignored file), as the workflow scans
#                           its checkout: its own rules and .gitleaks.toml's allowances, every
#                           finding's value redacted
# Usage: tools/verify/ci.sh
# Env:   TRIPBOXOSS_DK  the docker wrapper to call (default tools/private/dk.sh when it exists;
#                       a tree without those scripts gets plain docker with the same labels,
#                       the same 6g memory cap with no swap beyond it, and no network). A
#                       TRIPBOXOSS_DK that is not an executable file is an error.
# TRIPBOXOSS_DK and TRIPBOXOSS_CI_LOCK are for tests only: when either is set, stderr says
# `ci.sh: overrides in effect: N`, never their values.
# The run holds an exclusive lock on TRIPBOXOSS_CI_LOCK (default /tmp/tripboxoss-ci.lock) from
# before the build to the end, so two runs never retag the image under each other; narrow.sh holds
# it shared. The lock is flock(2) on the file, taken by util-linux's flock where it exists and by
# perl elsewhere (macOS has no flock); with neither, the run says so and goes on without it. If
# the image id differs after the last stage, nothing is certified and CI OK is not printed. The
# last line is `CI OK` when every stage passed.
set -euo pipefail
SELF="$(readlink -f "${BASH_SOURCE[0]}" 2>/dev/null || printf '%s' "${BASH_SOURCE[0]}")"
W="$(cd "$(dirname "$SELF")/../.." && pwd -P)"
IMAGE=tripboxoss:test
# the Dockerfile's own base, read from its runtime stage's FROM line (tag and digest), for the
# checks that run before the image is built
PYTHON="$(sed -n 's/^FROM \(python:[^ ]*\) AS runtime$/\1/p' "$W/Dockerfile")"
[ -n "$PYTHON" ] && [ "$(printf '%s\n' "$PYTHON" | wc -l)" -eq 1 ] \
  || { echo "ci.sh: the Dockerfile has no one 'FROM python:… AS runtime' line to read the Python image from" >&2; exit 2; }
ANY_USER=12345:54321  # a uid and gid the image has no account for
GITLEAKS=ghcr.io/gitleaks/gitleaks:v8.30.1@sha256:c00b6bd0aeb3071cbcb79009cb16a60dd9e0a7c60e2be9ab65d25e6bc8abbb7f
cd "$W"
# The test-only variables are counted, never shown, so a run that uses one says so.
OVERRIDES=0
for VAR in TRIPBOXOSS_DK TRIPBOXOSS_CI_LOCK; do
  if [ -n "${!VAR:-}" ]; then OVERRIDES=$((OVERRIDES + 1)); fi
done
if [ "$OVERRIDES" -gt 0 ]; then echo "ci.sh: overrides in effect: $OVERRIDES" >&2; fi

LOCK="${TRIPBOXOSS_CI_LOCK:-/tmp/tripboxoss-ci.lock}"
(umask 077; : >>"$LOCK") || { echo "ci.sh: cannot create the lock file" >&2; exit 2; }
exec 9>>"$LOCK"
# lock9 ex|sh [nb]: flock(2) on descriptor 9, which this shell keeps open, so the lock outlives the
# command that takes it. Exit 1 when nb was given and the lock is held; 3 when no tool can take it.
lock9() {
  if command -v flock >/dev/null 2>&1; then
    flock "-${1:0:1}" ${2:+-n} 9
  elif command -v perl >/dev/null 2>&1; then
    perl -MFcntl=:flock -e 'open(my $fd, ">>&=", 9) or exit 2;
      my $how = $ARGV[0] eq "sh" ? LOCK_SH : LOCK_EX; $how |= LOCK_NB if @ARGV > 1;
      flock($fd, $how) or exit 1' "$@"
  else
    return 3
  fi
}
held=0
lock9 ex nb || held=$?
if [ "$held" -eq 1 ]; then
  echo "ci.sh: waiting for another run to finish"
  lock9 ex || { echo "ci.sh: cannot take the lock" >&2; exit 2; }
elif [ "$held" -eq 3 ]; then
  echo "ci.sh: neither flock nor perl is installed; running without the lock" >&2
elif [ "$held" -ne 0 ]; then
  echo "ci.sh: cannot take the lock" >&2; exit 2
fi

dk_mode=plain
DK=""
if [ -n "${TRIPBOXOSS_DK:-}" ]; then
  [ -x "$TRIPBOXOSS_DK" ] && [ -f "$TRIPBOXOSS_DK" ] \
    || { echo "ci.sh: TRIPBOXOSS_DK is not an executable file" >&2; exit 2; }
  DK="$TRIPBOXOSS_DK"; dk_mode=guard
elif [ -f "$W/tools/private/dk.sh" ]; then
  DK="$W/tools/private/dk.sh"; dk_mode=guard
fi

# dkr build|run ARGS...: the wrapper when there is one, else docker with the labels the wrapper adds.
dkr() {
  if [ "$dk_mode" = guard ]; then "$DK" "$@"; return; fi
  local sub="$1"; shift
  case "$sub" in
    build) docker build --label program=tripboxoss "$@" ;;
    run)
      # 6g of memory and no swap beyond it, as the wrapper gives every container
      if [ "${TRIPBOXOSS_EGRESS:-}" = 1 ]; then
        docker run --rm --label program=tripboxoss --memory 6g --memory-swap 6g "$@"
      else
        docker run --rm --label program=tripboxoss --network none --memory 6g --memory-swap 6g "$@"
      fi ;;
    images) docker images --filter label=program=tripboxoss "$@" ;;
    *) docker "$sub" "$@" ;;
  esac
}

echo "== AGENTS.md byte budget =="
BYTES="$(( $(wc -c <AGENTS.md) ))"
[ "$BYTES" -le 40000 ] || { echo "AGENTS.md is $BYTES bytes, over the 40000-byte budget" >&2; exit 1; }
echo "AGENTS.md: $BYTES bytes of 40000"

# The checks before the build read the repository, read-only, in the Dockerfile's base image.
echo "== workflow rules =="
dkr run -v "$W:/repo:ro" -w /repo -e PYTHONDONTWRITEBYTECODE=1 "$PYTHON" \
  python tools/verify/check_workflows.py

echo "== licence headers =="
dkr run -v "$W:/repo:ro" -w /repo -e PYTHONDONTWRITEBYTECODE=1 "$PYTHON" \
  python tools/verify/spdx.py

echo "== test image =="
dkr build -t "$IMAGE" --target test "$W" || { echo "build of $IMAGE failed" >&2; exit 1; }

# The stages run on the tag, so the id it names must be the same before and after them.
image_id() {
  local out
  out="$(dkr images -q --no-trunc "$IMAGE")" || return 1
  [ -n "$out" ] && [ "$(printf '%s\n' "$out" | wc -l)" -eq 1 ] || return 1
  printf '%s\n' "$out"
}
BUILT_ID="$(image_id)" || { echo "ci.sh: cannot identify $IMAGE after the build" >&2; exit 1; }

echo "== dependency licences =="
dkr build -t tripboxoss:rt --target runtime "$W" || { echo "build of tripboxoss:rt failed" >&2; exit 1; }
SCAN=""
trap 'if [ -n "$SCAN" ]; then rm -rf "$SCAN"; fi' EXIT
dkr run --read-only --tmpfs /tmp -v "$W:/repo:ro" -w /repo -e PYTHONDONTWRITEBYTECODE=1 \
  tripboxoss:rt sh -c 'python /repo/tools/verify/licences.py --collect >/tmp/dists.json && python /repo/tools/verify/licences.py --dists /tmp/dists.json'

echo "== lint =="
dkr run "$IMAGE" ruff check src tests
echo "== tests =="
dkr run --user "$ANY_USER" -e PYTHONDONTWRITEBYTECODE=1 "$IMAGE" pytest -q -p no:cacheprovider

echo "== a new install as any user =="
dkr run --user "$ANY_USER" --read-only --tmpfs /tmp -v "$W:/repo:ro" -w /repo \
  -e PYTHONDONTWRITEBYTECODE=1 -e TRIPBOX_DATA_DIR=/tmp/data -e TRIPBOX_LOGS_DIR=/tmp/logs \
  -e TRIPBOX_ARCHIVE_DIR=/tmp/archive -e TRIPBOX_CONFIG_DIR=/tmp/config \
  "$IMAGE" python /repo/tools/verify/standalone_smoke.py

echo "== corpus scorecard =="
dkr run --user "$ANY_USER" --read-only --tmpfs /tmp "$IMAGE" python -m tripbox eval --recorded

# The whole repository, read-only: tools/ tests need tools/private and, for the wrapper scripts' own tests,
# the .git directory. Neither the bytecode nor the caches may be written there.
echo "== tools lint =="
dkr run -v "$W:/repo:ro" -w /repo -e PYTHONDONTWRITEBYTECODE=1 "$IMAGE" ruff check --no-cache tools
echo "== tools tests =="
dkr run -v "$W:/repo:ro" -w /repo -e PYTHONDONTWRITEBYTECODE=1 -e TRIPBOXOSS_STRICT=1 "$IMAGE" \
  pytest -q -p no:cacheprovider tools

# The demo's static copy, made in the image as any user and checked before it is published
echo "== demo site =="
dkr run --user "$ANY_USER" --read-only --tmpfs /tmp -v "$W:/repo:ro" -w /repo \
  -e PYTHONDONTWRITEBYTECODE=1 "$IMAGE" \
  sh -c 'tripbox demo export --out /tmp/site --today 2027-06-16 && python /repo/tools/verify/demo_site_check.py /tmp/site'

# gitleaks's own rules and the allowances of .gitleaks.toml, each with its reason, over what a
# checkout of the next commit holds, as the workflow scans its checkout: the files git tracks, as
# they stand in the working tree, and the new ones it does not ignore, copied into a scratch
# folder. A file .gitignore keeps out (notes, caches, scratch) is never read; gitleaks itself
# would read it. A tree that is no git work tree is scanned whole.
echo "== secrets =="
dkr pull "$GITLEAKS"
SCANNED="$W"
if git -C "$W" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  SCAN="$(mktemp -d "${TMPDIR:-/tmp}/tripbox-secrets.XXXXXX")"
  chmod 755 "$SCAN"  # gitleaks's image reads it as a user of its own
  git -C "$W" ls-files -z --cached --others --exclude-standard \
    | (cd "$W" && while IFS= read -r -d '' f; do
         if [ -e "$f" ] || [ -L "$f" ]; then printf '%s\0' "$f"; fi
       done) \
    | tar -C "$W" --null --no-recursion -T - -cf - | tar -C "$SCAN" -xf - \
    || { echo "ci.sh: the files to scan could not be copied" >&2; exit 1; }
  SCANNED="$SCAN"
fi
dkr run -v "$SCANNED:/repo:ro" "$GITLEAKS" dir /repo --config /repo/.gitleaks.toml --redact --no-banner --exit-code 1

AFTER_ID="$(image_id)" || AFTER_ID=""
if [ -z "$AFTER_ID" ] || [ "$AFTER_ID" != "$BUILT_ID" ]; then
  echo "ci.sh: the test image changed during this run; nothing is certified" >&2
  exit 1
fi
echo "CI OK"
