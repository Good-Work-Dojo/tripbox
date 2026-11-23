#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
# One pytest run of the worktree's code in the existing tripboxoss:test image, with the whole
# repository mounted read-only at /repo as the working directory and PYTHONPATH=/repo/src, so paths
# are given relative to the repository root.
# Usage: tools/verify/narrow.sh <pytest args...>
#        tools/verify/narrow.sh tests/test_x.py -k name
#        tools/verify/narrow.sh tools/verify/test_spdx.py
# Exit 2 on a usage error or a missing image (run tools/verify/ci.sh first); otherwise pytest's own
# status. Env: TRIPBOXOSS_DK and TRIPBOXOSS_CI_LOCK, as for ci.sh (for tests only: when either is
# set, stderr says `narrow.sh: overrides in effect: N`); the run holds the lock shared (flock(1),
# else perl, else none, as in ci.sh).
set -euo pipefail
SELF="$(readlink -f "${BASH_SOURCE[0]}" 2>/dev/null || printf '%s' "${BASH_SOURCE[0]}")"
W="$(cd "$(dirname "$SELF")/../.." && pwd -P)"
IMAGE=tripboxoss:test
[ "$#" -ge 1 ] || { echo "usage: tools/verify/narrow.sh <pytest args...>" >&2; exit 2; }
case "$W" in *:*) echo "narrow.sh: the repository path contains a colon: $W" >&2; exit 2 ;; esac
# The test-only variables are counted, never shown, so a run that uses one says so.
OVERRIDES=0
for VAR in TRIPBOXOSS_DK TRIPBOXOSS_CI_LOCK; do
  if [ -n "${!VAR:-}" ]; then OVERRIDES=$((OVERRIDES + 1)); fi
done
if [ "$OVERRIDES" -gt 0 ]; then echo "narrow.sh: overrides in effect: $OVERRIDES" >&2; fi

LOCK="${TRIPBOXOSS_CI_LOCK:-/tmp/tripboxoss-ci.lock}"
(umask 077; : >>"$LOCK") || { echo "narrow.sh: cannot create the lock file" >&2; exit 2; }
exec 9>>"$LOCK"
# shared: runs may overlap each other, never a ci.sh build
if command -v flock >/dev/null 2>&1; then
  flock -s 9
elif command -v perl >/dev/null 2>&1; then
  perl -MFcntl=:flock -e 'open(my $fd, ">>&=", 9) or exit 2; flock($fd, LOCK_SH) or exit 1' \
    || { echo "narrow.sh: cannot take the lock" >&2; exit 2; }
else
  echo "narrow.sh: neither flock nor perl is installed; running without the lock" >&2
fi

DK=""
if [ -n "${TRIPBOXOSS_DK:-}" ]; then
  [ -x "$TRIPBOXOSS_DK" ] && [ -f "$TRIPBOXOSS_DK" ] \
    || { echo "narrow.sh: TRIPBOXOSS_DK is not an executable file" >&2; exit 2; }
  DK="$TRIPBOXOSS_DK"
fi
# dkr run|images ARGS...: TRIPBOXOSS_DK when it is set, else docker as ci.sh calls it
dkr() {
  if [ -n "$DK" ]; then "$DK" "$@"; return; fi
  local sub="$1"; shift
  case "$sub" in
    run) docker run --rm --label program=tripboxoss --network none --memory 6g --memory-swap 6g "$@" ;;
    images) docker images --filter label=program=tripboxoss "$@" ;;
    *) docker "$sub" "$@" ;;
  esac
}

present="$(dkr images -q "$IMAGE" 2>/dev/null || true)"
[ -n "$present" ] || { echo "$IMAGE is missing: run tools/verify/ci.sh first" >&2; exit 2; }
dkr run -v "$W:/repo:ro" -w /repo -e PYTHONPATH=/repo/src -e PYTHONDONTWRITEBYTECODE=1 "$IMAGE" \
  pytest -q -p no:cacheprovider "$@"
