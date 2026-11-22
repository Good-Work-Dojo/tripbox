#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
# Says whether the Trips button's patch (trips-dock.patch, beside this file) still applies to an
# AirTrail release: by default the newest one, the highest v<major>.<minor>.<patch> tag of
# AirTrail's repository. Only the two files the patch changes are fetched (a shallow, sparse
# clone), and nothing is built. Needs bash, git and, with --open-issue, gh.
# Usage: integrations/airtrail/patch-check.sh [--tag vX.Y.Z] [--open-issue]
#   --tag vX.Y.Z  check this release instead of the newest
#   --open-issue  when it does not apply, open an issue in this repository saying so (gh, with
#                 GH_TOKEN), unless an open one with the same title is there already
# Env: AIRTRAIL_REPO  the repository to ask (default https://github.com/johanohly/AirTrail.git)
# The last line is `patch check: <tag> applies` (exit 0) or `patch check: <tag> FAILS` (exit 1);
# a bad argument, or AirTrail's tags or files that cannot be fetched, exits 2.
set -uo pipefail

HERE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd -P)"
PATCH="$HERE/trips-dock.patch"
FILES=(src/lib/components/NavigationDock.svelte src/lib/components/dock/DockTooltipItem.svelte)
REPO="${AIRTRAIL_REPO:-https://github.com/johanohly/AirTrail.git}"
RELEASE='^v[0-9]+\.[0-9]+\.[0-9]+$'

usage() {
  echo "usage: integrations/airtrail/patch-check.sh [--tag vX.Y.Z] [--open-issue]" >&2
  exit 2
}
stop() { echo "patch check: $*" >&2; exit 2; }

TAG="" ISSUE=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --tag) [ "$#" -ge 2 ] && [ -z "$TAG" ] || usage; TAG="$2"; shift 2 ;;
    --open-issue) [ "$ISSUE" -eq 0 ] || usage; ISSUE=1; shift ;;
    *) usage ;;
  esac
done
if [ -n "$TAG" ] && ! [[ "$TAG" =~ $RELEASE ]]; then usage; fi

export GIT_TERMINAL_PROMPT=0
if [ -z "$TAG" ]; then
  LISTED="$(git ls-remote --tags --refs -- "$REPO")" || stop "cannot list AirTrail's tags"
  TAG="$(printf '%s\n' "$LISTED" | while read -r _sha ref; do
      name="${ref#refs/tags/}"
      if [[ "$name" =~ $RELEASE ]]; then printf '%s\n' "${name#v}"; fi
    done | sort -t . -k1,1n -k2,2n -k3,3n | tail -n 1)"
  [ -n "$TAG" ] || stop "AirTrail's repository lists no release tag"
  TAG="v$TAG"
fi

WORK="$(mktemp -d)" || stop "cannot make a temporary directory"
trap 'rm -rf -- "$WORK"' EXIT
git -c advice.detachedHead=false clone --quiet --depth 1 --branch "$TAG" --filter=blob:none \
  --sparse -- "$REPO" "$WORK/airtrail" \
  && git -C "$WORK/airtrail" sparse-checkout set --no-cone "${FILES[@]/#//}" \
  || stop "cannot fetch AirTrail $TAG"

if git -C "$WORK/airtrail" apply --check "$PATCH"; then
  echo "patch check: $TAG applies"
  exit 0
fi

if [ "$ISSUE" -eq 1 ]; then
  TITLE="The dock patch no longer applies to AirTrail $TAG"
  if ! OPEN="$(gh issue list --state open --limit 500 --json title --jq '.[].title')"; then
    echo "patch check: cannot list this repository's open issues" >&2
  elif printf '%s\n' "$OPEN" | grep -qxF -- "$TITLE"; then
    echo "patch check: an open issue says so already" >&2
  else
    BODY="integrations/airtrail/trips-dock.patch no longer applies to AirTrail $TAG.
It adds lines to two of AirTrail's files, which have changed:
- ${FILES[0]}
- ${FILES[1]}

To see where it fails, run:

    integrations/airtrail/patch-check.sh --tag $TAG

then write the patch again against $TAG and update the release it names."
    gh issue create --title "$TITLE" --body "$BODY" >/dev/null \
      || echo "patch check: cannot open the issue" >&2
  fi
fi
echo "patch check: $TAG FAILS"
exit 1
