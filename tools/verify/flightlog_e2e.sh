#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
# The end-to-end run against a throwaway stock AirTrail, on a machine where a workflow may start
# containers: starts the stack in tools/verify/airtrail/compose.yml under the project tripbox-e2e
# (AirTrail, its Postgres in a tmpfs and an idle container of the test image), inserts five fresh
# API keys (four for the instance's owner, picked by that mark and never by id order, so a
# full-access key reaches every flight: the six scopes tripbox uses; the owner's own hands, which
# may also delete; the six less the passengers scope; the six and the three `.any` scopes of a
# full-access key; and one with the six for the first other user by id), runs
# tests/test_flightlog_e2e.py and tests/test_flightlog_matrix.py in the test container, and removes
# the stack again, all in this one command. It needs Docker with compose, curl and python3, and
# takes minutes: AirTrail downloads a public airport table on its first start.
# Usage: TRIPBOX_SMOKE=1 tools/verify/flightlog_e2e.sh [--image REF]
#   REF  the tripbox test image (the Dockerfile's `test` target; default tripboxoss:test)
# Without TRIPBOX_SMOKE=1 it says what it would start and exits 2, before anything runs. AirTrail
# is published on 127.0.0.1:18090. No COMPOSE_* or E2E_* variable of the caller's reaches compose.
# Steps, each named by its failure line: database (Postgres up and healthy), start (AirTrail and
# the test container), wait (AirTrail answers on the loopback port, at most 300 s), key (the API
# keys inserted), tests (pytest in the test container). The last line is `flight log e2e: ok`
# (exit 0) or `flight log e2e: FAIL <step>` (exit 1); a stack that cannot be taken down again fails
# as teardown. The keys are generated here and passed only in the environment, by name, never as an
# argument, and no line printed here holds one.
set -uo pipefail

W="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../.." && pwd -P)"
PROJECT=tripbox-e2e
PORT=18090
usage() {
  echo "usage: TRIPBOX_SMOKE=1 tools/verify/flightlog_e2e.sh [--image REF]" >&2
  exit 2
}
IMAGE=tripboxoss:test
if [ "$#" -gt 0 ]; then
  [ "$#" -eq 2 ] && [ "$1" = --image ] || usage
  IMAGE="$2"
fi
[[ "$IMAGE" =~ ^[A-Za-z0-9][A-Za-z0-9._/:@-]*$ ]] || usage
if [ "${TRIPBOX_SMOKE:-}" != 1 ]; then
  echo "flight log e2e: this starts and removes a stack named $PROJECT on 127.0.0.1:$PORT;" \
    "set TRIPBOX_SMOKE=1 to run it" >&2
  exit 2
fi

for VAR in $(compgen -e); do
  case "$VAR" in COMPOSE_* | E2E_*) unset "$VAR" ;; esac
done
COMPOSE="$W/tools/verify/airtrail/compose.yml"
export E2E_AIRTRAIL_PORT="$PORT"
export E2E_TRIPBOX_IMAGE="$IMAGE"
new_key() { python3 -c 'import secrets; print(secrets.token_urlsafe(32))'; }
for name in E2E_AIRTRAIL_API_KEY E2E_AIRTRAIL_HAND_KEY E2E_AIRTRAIL_NARROW_KEY \
  E2E_AIRTRAIL_FULL_KEY E2E_AIRTRAIL_OTHER_KEY; do
  value="$(new_key)" && [ -n "$value" ] || { echo "flight log e2e: FAIL key"; exit 1; }
  printf -v "$name" '%s' "$value"
  export "${name?}"
done
unset value

# What tripbox needs; the owner's hands add deleting; the narrow key lacks the passengers scope;
# the full key adds the `.any` scopes a key made before AirTrail had scopes held.
SIX=profile.read,reference_data.read,flight.read.own,flight.create.own,flight.update.own
NARROW="$SIX"
SIX="$SIX,flight.passengers.manage.own"
HAND="$SIX,flight.delete.own"
FULL="$SIX,flight.read.any,flight.update.any,flight.passengers.manage.any"

dc() { docker compose -p "$PROJECT" -f "$COMPOSE" "$@"; }

# The step running now; empty once every step has passed.
STEP=database
finish() {
  local rc=$?
  trap - EXIT
  if ! dc down -v --remove-orphans; then
    echo "flightlog_e2e.sh: the stack is still up; take it down with" \
      "docker compose -p $PROJECT -f $COMPOSE down -v --remove-orphans" >&2
    STEP="${STEP:-teardown}"
  fi
  if [ -z "$STEP" ] && [ "$rc" -eq 0 ]; then
    echo "flight log e2e: ok"
    exit 0
  fi
  echo "flight log e2e: FAIL ${STEP:-teardown}"
  exit 1
}
trap finish EXIT
trap 'exit 130' INT TERM

dc up -d --wait flightlogdb || exit 1

STEP=start
dc up -d flightlog e2e || exit 1

# AirTrail answers once its start-up (migrations, the airport download, the demo seed) is done.
STEP=wait
deadline=$((SECONDS + 300))
until code="$(curl -s -o /dev/null --max-time 10 -w '%{http_code}' \
  "http://127.0.0.1:$PORT/")" && [ "$code" -ge 200 ] && [ "$code" -le 399 ]; do
  [ "$SECONDS" -lt "$deadline" ] || exit 1
  sleep 3
done

STEP=key
key() {  # key <variable> <name> <who> <scopes>
  python3 "$W/tools/verify/airtrail_test_key.py" flightlogdb "$3" --key-env "$1" --name "$2" \
    --scopes "$4" --exec-prefix "docker compose -p $PROJECT -f $(printf '%q' "$COMPOSE") exec -T"
}
key E2E_AIRTRAIL_API_KEY tripbox-e2e --owner "$SIX" || exit 1
key E2E_AIRTRAIL_HAND_KEY tripbox-e2e-hand --owner "$HAND" || exit 1
key E2E_AIRTRAIL_NARROW_KEY tripbox-e2e-narrow --owner "$NARROW" || exit 1
key E2E_AIRTRAIL_FULL_KEY tripbox-e2e-full --owner "$FULL" || exit 1
key E2E_AIRTRAIL_OTHER_KEY tripbox-e2e-other --other-user "$SIX" || exit 1

# pyproject's addopts already holds -q; a second one would silence pytest's `N passed` line
STEP=tests
dc exec -T e2e python -m pytest -p no:cacheprovider -rN --tb=short \
  /app/tests/test_flightlog_e2e.py /app/tests/test_flightlog_matrix.py || exit 1

STEP=""
exit 0
