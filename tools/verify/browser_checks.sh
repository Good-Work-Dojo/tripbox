#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
# The screenshot stack's checks, on a machine where a workflow may start containers: the four
# commands tools/verify/README.md gives for screens/, run in a new scratch folder over a demo
# seeded for the day, under the project tripbox-screens, which publishes no port. It needs Docker
# with compose and the image named; the browser image is built from tools/verify/screens/.
# Usage: TRIPBOX_SMOKE=1 tools/verify/browser_checks.sh [--image REF]
#   REF  the tripbox image the page and the seed run (default tripboxoss:test, the gate's)
# Without TRIPBOX_SMOKE=1 it says what it would start and exits 2, before anything runs.
# No TRIPBOX_*, COMPOSE_* or SCREENS_* variable of the caller's reaches compose: the stack is given
# SCREENS_TRIPBOX_IMAGE (REF), SCREENS_BROWSER_IMAGE (tripbox-screens-browser:local) and
# SCREENS_USER (this user's uid:gid, which owns demo/ and out/), and its own defaults for the rest.
# Steps, each named by its failure line:
#   copy      compose.yml, shoot.py and checks.py copied into the scratch folder (under
#             RUNNER_TEMP, else TMPDIR, else /tmp), with an empty out/
#   seed      `tripbox demo seed --out demo` in a container of REF as this user, with no network
#   browser   the browser image built
#   start     `docker compose up -d --wait web browser`
#   shoot     shoot.py run in the browser; its summary must be `checks: 8 of 8 ok`
#   teardown  `docker compose down -v --remove-orphans`, always once the stack may be up, and the
#             scratch folder removed
# The last line is `browser checks: ok` (exit 0) or `browser checks: FAIL <step>` (exit 1).
set -uo pipefail

W="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../.." && pwd -P)"
PROJECT=tripbox-screens
BROWSER=tripbox-screens-browser:local
CHECKS="checks: 8 of 8 ok"
usage() {
  echo "usage: TRIPBOX_SMOKE=1 tools/verify/browser_checks.sh [--image REF]" >&2
  exit 2
}
IMAGE=tripboxoss:test
if [ "$#" -gt 0 ]; then
  [ "$#" -eq 2 ] && [ "$1" = --image ] || usage
  IMAGE="$2"
fi
[[ "$IMAGE" =~ ^[A-Za-z0-9][A-Za-z0-9._/:@-]*$ ]] || usage
if [ "${TRIPBOX_SMOKE:-}" != 1 ]; then
  echo "browser checks: this starts and removes a stack named $PROJECT, which publishes no port;" \
    "set TRIPBOX_SMOKE=1 to run it" >&2
  exit 2
fi

for VAR in $(compgen -e); do
  case "$VAR" in TRIPBOX_* | COMPOSE_* | SCREENS_*) unset "$VAR" ;; esac
done
SCRATCH="${RUNNER_TEMP:-${TMPDIR:-/tmp}}"
DIR="$(mktemp -d "$SCRATCH/tripbox-screens.XXXXXX")" || { echo "browser checks: FAIL copy"; exit 1; }
export SCREENS_TRIPBOX_IMAGE="$IMAGE" SCREENS_BROWSER_IMAGE="$BROWSER"
SCREENS_USER="$(id -u):$(id -g)"
export SCREENS_USER
compose() { (cd "$DIR" && docker compose -p "$PROJECT" "$@"); }

STEP=copy
UP=0
finish() {
  local rc=$?
  trap - EXIT
  if [ "$UP" -eq 1 ] && ! compose down -v --remove-orphans; then
    echo "browser_checks.sh: the stack is still up; take it down with" \
      "docker compose -p $PROJECT down -v --remove-orphans" >&2
    STEP="${STEP:-teardown}"
  fi
  if ! rm -rf "$DIR"; then
    echo "browser_checks.sh: $DIR is left behind" >&2
    STEP="${STEP:-teardown}"
  fi
  if [ -z "$STEP" ] && [ "$rc" -eq 0 ]; then
    echo "browser checks: ok"
    exit 0
  fi
  echo "browser checks: FAIL ${STEP:-teardown}"
  exit 1
}
trap finish EXIT
trap 'exit 130' INT TERM

for f in compose.yml shoot.py checks.py; do
  cp "$W/tools/verify/screens/$f" "$DIR/$f" || exit 1
done
mkdir "$DIR/out" || exit 1

STEP=seed
docker run --rm --network none --user "$SCREENS_USER" -v "$DIR:/work" "$IMAGE" \
  python -m tripbox demo seed --out /work/demo || exit 1

STEP=browser
docker build -t "$BROWSER" "$W/tools/verify/screens" || exit 1

STEP=start
UP=1
compose up -d --wait web browser || exit 1

STEP=shoot
compose exec -T browser python3 /harness/shoot.py --base-url "http://web:8110" --out /out \
  | tee "$DIR/shoot.txt" || exit 1
grep -qxF "$CHECKS" "$DIR/shoot.txt" || exit 1

STEP=teardown
compose down -v --remove-orphans || exit 1
UP=0
STEP=""
exit 0
