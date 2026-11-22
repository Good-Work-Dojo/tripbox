#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
# Builds AirTrail with the Trips button in its dock: AirTrail's own source at a release tag, the
# patch beside this file applied with the trips page's address put in, and AirTrail's own
# Dockerfile. Needs bash, git and docker (and the network, to fetch AirTrail and what it installs).
# Usage: integrations/airtrail/build.sh <AirTrail tag> <trips URL> <image tag>
#   <AirTrail tag>  a release, v<major>.<minor>.<patch>, such as v3.14.0
#   <trips URL>     where the button leads: an address such as https://trips.example.com, or a path
#                   such as /tripbox when tripbox is served under AirTrail's own hostname; letters,
#                   digits and . _ ~ / : - only, and neither / alone nor beginning //
#   <image tag>     the tag the image is built as, such as airtrail-trips:3.14.0
# Env: AIRTRAIL_REPO         the repository to clone (default https://github.com/johanohly/AirTrail.git)
#      AIRTRAIL_BUILD_DIR    where to clone it, absent or empty (default a new temporary directory,
#                            removed at the end)
#      AIRTRAIL_BASE_IMAGES  a file of lines `<image as the Dockerfile writes it> <image to build
#                            from>`: each replaces that image on every FROM line that names it (to
#                            build from images pinned by digest); a line that matches none fails
# Steps, each named by its failure line: clone, patch, bases (only with AIRTRAIL_BASE_IMAGES),
# image. The last line on stdout is `build: ok <image tag>` (exit 0) or `build: FAIL <step>`
# (exit 1); a bad argument exits 2 before anything runs. On success it ends by reminding you to
# back up AirTrail's database: AirTrail upgrades it on every start.
set -uo pipefail

HERE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd -P)"
PATCH="$HERE/trips-dock.patch"
PLACEHOLDER=__TRIPBOX_TRIPS_URL__
DEFAULT_REPO=https://github.com/johanohly/AirTrail.git

usage() {
  echo "usage: integrations/airtrail/build.sh <AirTrail tag> <trips URL> <image tag>" >&2
  exit 2
}
bad() { echo "build: $*" >&2; exit 2; }

[ "$#" -eq 3 ] || usage
TAG="$1" URL="$2" IMAGE="$3"
[[ "$TAG" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] \
  || bad "the AirTrail tag must be a release such as v3.14.0"
# not / alone, and not //host/...: a browser reads that as another host's address
if ! [[ "$URL" =~ ^(https?://[A-Za-z0-9._~/:-]+|/[A-Za-z0-9._~/:-]*)$ ]] || [ "$URL" = / ] \
  || [[ "$URL" == //* ]]; then
  bad "the trips URL must be an address such as https://trips.example.com or a path such as" \
    "/tripbox"
fi
[[ "$IMAGE" =~ ^[a-z0-9][a-z0-9._/-]*(:[A-Za-z0-9_][A-Za-z0-9._-]{0,127})?$ ]] \
  || bad "the image tag must be a name such as airtrail-trips:3.14.0"
REPO="${AIRTRAIL_REPO:-$DEFAULT_REPO}"
BASES="${AIRTRAIL_BASE_IMAGES:-}"
if [ -n "$BASES" ]; then
  [ -f "$BASES" ] && [ -r "$BASES" ] || bad "AIRTRAIL_BASE_IMAGES names no file it can read"
fi
# the release the patch was written for, as its header says
WRITTEN_FOR="$(grep -m1 -oE '^Written for AirTrail v[0-9]+\.[0-9]+\.[0-9]+' "$PATCH" 2>/dev/null \
  | grep -oE 'v[0-9.]+$' || true)"
[ -n "$WRITTEN_FOR" ] || bad "the patch beside this script does not say which release it is for"

WORK="$(mktemp -d)" || bad "cannot make a temporary directory"
CLONE="${AIRTRAIL_BUILD_DIR:-$WORK/airtrail}"
STEP=clone
finish() {
  local rc=$?
  trap - EXIT
  rm -rf -- "$WORK"
  if [ -z "$STEP" ] && [ "$rc" -eq 0 ]; then
    echo "build: ok $IMAGE"
    echo "Back up AirTrail's database before you start this image: AirTrail upgrades its" \
      "database on every start." >&2
    exit 0
  fi
  echo "build: FAIL $STEP"
  exit 1
}
trap finish EXIT
trap 'exit 130' INT TERM

if [ -e "$CLONE" ] && [ -n "$(ls -A -- "$CLONE" 2>/dev/null || echo x)" ]; then
  echo "build: AIRTRAIL_BUILD_DIR must be absent or empty: $CLONE" >&2
  exit 1
fi
GIT_TERMINAL_PROMPT=0 git -c advice.detachedHead=false clone --quiet --depth 1 --branch "$TAG" \
  -- "$REPO" "$CLONE" || exit 1

# The patch with the trips page's address in its placeholder's place, beside the clone.
STEP=patch
while IFS= read -r line || [ -n "$line" ]; do
  printf '%s\n' "${line//"$PLACEHOLDER"/"$URL"}"
done <"$PATCH" >"$WORK/trips-dock.patch" || exit 1
if ! git -C "$CLONE" apply --check "$WORK/trips-dock.patch" \
  || ! git -C "$CLONE" apply "$WORK/trips-dock.patch"; then
  echo "build: the patch does not apply to AirTrail $TAG; it was written for $WRITTEN_FOR" >&2
  exit 1
fi

# Each image a line of AIRTRAIL_BASE_IMAGES names is replaced on every FROM line that names it
# (after any --platform= flag), and nothing else of the line changes.
DOCKERFILE="$CLONE/docker/Dockerfile"
FROM_LINE='^([[:space:]]*[Ff][Rr][Oo][Mm][[:space:]]+(--[^[:space:]]+[[:space:]]+)*)([^[:space:]]+)(.*)$'
if [ -n "$BASES" ]; then
  STEP=bases
  mapfile -t LINES <"$DOCKERFILE" || exit 1
  while read -r FROM TO EXTRA || [ -n "${FROM:-}" ]; do
    case "${FROM:-}" in "" | "#"*) continue ;; esac
    if [ -z "${TO:-}" ] || [ -n "${EXTRA:-}" ] || ! [[ "$TO" =~ ^[A-Za-z0-9._/:@-]+$ ]]; then
      echo "build: a line of AIRTRAIL_BASE_IMAGES is not <image> <image to build from>" >&2
      exit 1
    fi
    FOUND=0
    for i in "${!LINES[@]}"; do
      if [[ "${LINES[i]}" =~ $FROM_LINE ]] && [ "${BASH_REMATCH[3]}" = "$FROM" ]; then
        LINES[i]="${BASH_REMATCH[1]}$TO${BASH_REMATCH[4]}"
        FOUND=1
      fi
    done
    if [ "$FOUND" -eq 0 ]; then
      echo "build: no FROM line of docker/Dockerfile names $FROM" >&2
      exit 1
    fi
  done <"$BASES"
  printf '%s\n' "${LINES[@]}" >"$DOCKERFILE" || exit 1
fi

STEP=image
(cd "$CLONE" && docker build -f docker/Dockerfile -t "$IMAGE" .) || exit 1

STEP=""
exit 0
