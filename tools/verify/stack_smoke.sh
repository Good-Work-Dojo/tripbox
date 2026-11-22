#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
# The quick start's default path, as a stranger takes it, on a machine where a workflow may start
# containers: builds the install bundle, unpacks it into a new scratch folder, writes the one
# settings line the quick start writes, starts the stack with compose under the project
# tripbox-smoke from the unpacked folder, checks the page as its first user, runs `doctor`, checks
# what the stack publishes and how it restarts, and takes the stack down again whatever happened.
# It needs Docker with compose, curl and python3, and the image named.
# Usage: TRIPBOX_SMOKE=1 tools/verify/stack_smoke.sh --image REF
#   REF  the tripbox image the stack runs (given to compose as TRIPBOX_IMAGE, never written to
#        the settings); it is used where it is, never pulled by name here
# Without TRIPBOX_SMOKE=1 it says what it would start and exits 2; so it does when 127.0.0.1:8110
# answers or accepts a connection (`stack smoke: 127.0.0.1:8110 is in use`), before anything runs.
# No TRIPBOX_* or COMPOSE_* variable of the caller's reaches compose.
# Steps, each named by its failure line:
#   bundle     tools/maintainer/install_bundle.py writes tripbox.tar.gz into the scratch folder
#              (under RUNNER_TEMP, else TMPDIR, else /tmp)
#   unpack     the bundle unpacked there; when the user running this is not 1000, the four
#              folders the services write are handed to 1000 by a container of the image running
#              as root (docs/install.md: "chown the four folders to 1000"), and taken back the
#              same way at the end
#   settings   .env is the one line `TRIPBOX_PASSWORD=<a generated password>`, mode 600; the
#              password holds a $, written $$ there as the quick start says
#   start      `docker compose -p tripbox-smoke up -d --wait`
#   page       tools/verify/quickstart_check.py at http://localhost:8110 with the README's block
#              `sample`, signed in with the password as typed
#   doctor     `docker compose run --rm worker tripbox doctor` exits 0
#   published  the page's port is published on 127.0.0.1 alone and no other container publishes
#              one; every container's restart policy is unless-stopped
#   teardown   `docker compose down -v --remove-orphans`, always once the stack may be up, and the
#              scratch folder removed
# The last line is `stack smoke: ok` (exit 0) or `stack smoke: FAIL <step>` (exit 1).
set -uo pipefail

W="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../.." && pwd -P)"
PROJECT=tripbox-smoke
ADDRESS=127.0.0.1:8110
URL=http://localhost:8110
FOLDERS=(data archive logs backups)
usage() {
  echo "usage: TRIPBOX_SMOKE=1 tools/verify/stack_smoke.sh --image REF" >&2
  exit 2
}
[ "$#" -eq 2 ] && [ "$1" = --image ] || usage
IMAGE="$2"
[[ "$IMAGE" =~ ^[A-Za-z0-9][A-Za-z0-9._/:@-]*$ ]] || usage
if [ "${TRIPBOX_SMOKE:-}" != 1 ]; then
  echo "stack smoke: this starts and removes a stack named $PROJECT on $ADDRESS;" \
    "set TRIPBOX_SMOKE=1 to run it" >&2
  exit 2
fi
# curl's 7 is a connection refused: anything else (an answer, a silence, an empty reply) means
# something holds the port
curl -s -o /dev/null --max-time 5 "http://$ADDRESS/"
if [ "$?" -ne 7 ]; then
  echo "stack smoke: $ADDRESS is in use" >&2
  exit 2
fi

# what compose reads comes from the settings file alone, and the image named here
for VAR in $(compgen -e); do
  case "$VAR" in TRIPBOX_* | COMPOSE_*) unset "$VAR" ;; esac
done
SCRATCH="${RUNNER_TEMP:-${TMPDIR:-/tmp}}"
DIR="$(mktemp -d "$SCRATCH/tripbox-smoke.XXXXXX")" || { echo "stack smoke: FAIL bundle"; exit 1; }
INSTALL="$DIR/tripbox"
compose() { (cd "$INSTALL" && TRIPBOX_IMAGE="$IMAGE" docker compose -p "$PROJECT" "$@"); }
as_root() { docker run --rm --network none --user 0:0 -v "$DIR:/work" "$IMAGE" "$@"; }

STEP=bundle
UP=0
GIVEN=0
finish() {
  local rc=$?
  trap - EXIT
  if [ "$UP" -eq 1 ] && ! compose down -v --remove-orphans; then
    echo "stack_smoke.sh: the stack is still up; take it down with" \
      "docker compose -p $PROJECT down -v --remove-orphans" >&2
    STEP="${STEP:-teardown}"
  fi
  if [ "$GIVEN" -eq 1 ] && ! as_root rm -rf /work/tripbox; then
    echo "stack_smoke.sh: the install's folders in $DIR could not be removed" >&2
    STEP="${STEP:-teardown}"
  fi
  if ! rm -rf "$DIR"; then
    echo "stack_smoke.sh: $DIR is left behind" >&2
    STEP="${STEP:-teardown}"
  fi
  if [ -z "$STEP" ] && [ "$rc" -eq 0 ]; then
    echo "stack smoke: ok"
    exit 0
  fi
  echo "stack smoke: FAIL ${STEP:-teardown}"
  exit 1
}
trap finish EXIT
trap 'exit 130' INT TERM

python3 -I "$W/tools/maintainer/install_bundle.py" --out "$DIR/tripbox.tar.gz" || exit 1

STEP=unpack
tar --no-same-owner -xzf "$DIR/tripbox.tar.gz" -C "$DIR" && [ -f "$INSTALL/compose.yaml" ] || exit 1
if [ "$(id -u)" != 1000 ]; then
  GIVEN=1
  as_root chown 1000:1000 "${FOLDERS[@]/#//work/tripbox/}" || exit 1
fi

STEP=settings
# a password with a $ before a letter, which compose would read as a variable unless written $$
PASSWORD="$(python3 -I -c 'import secrets; print(secrets.token_urlsafe(18) + "$Q" + secrets.token_urlsafe(6))')" \
  || exit 1
WRITTEN="${PASSWORD//\$/\$\$}"
(cd "$INSTALL" && umask 077 && printf 'TRIPBOX_PASSWORD=%s\n' "$WRITTEN" >.env) || exit 1
(umask 077 && printf '%s\n' "$PASSWORD" >"$DIR/password") || exit 1
unset PASSWORD WRITTEN

STEP=start
UP=1
compose up -d --wait || exit 1

STEP=page
(umask 077 && python3 -I "$W/tools/verify/readme_blocks.py" "$W/README.md" sample >"$DIR/sample.txt") \
  || exit 1
python3 -I "$W/tools/verify/quickstart_check.py" --base-url "$URL" \
  --password-file "$DIR/password" --sample "$DIR/sample.txt" || exit 1

STEP=doctor
compose run --rm worker tripbox doctor || exit 1

STEP=published
IDS="$(compose ps -q)" || exit 1
mapfile -t CONTAINERS < <(printf '%s\n' "$IDS" | grep -v '^$')
[ "${#CONTAINERS[@]}" -gt 0 ] || { echo "stack_smoke.sh: the stack runs no container" >&2; exit 1; }
for ID in "${CONTAINERS[@]}"; do
  [[ "$ID" =~ ^[0-9a-f]{64}$ ]] || { echo "stack_smoke.sh: compose gave no container id" >&2; exit 1; }
done
docker inspect "${CONTAINERS[@]}" | python3 -I -c '
import json, sys
HOST_IP, HOST_PORT = "HostIp", "HostPort"
try:
    found = json.load(sys.stdin)
except ValueError:
    sys.exit("stack_smoke.sh: docker inspect printed no JSON")
want = int(sys.argv[1])
if not isinstance(found, list) or len(found) != want:
    sys.exit(f"stack_smoke.sh: docker inspect did not describe the {want} containers")
bad, services = 0, set()
for c in found:
    host = c.get("HostConfig") or {}
    name = str(c.get("Name") or "?").lstrip("/")
    service = ((c.get("Config") or {}).get("Labels") or {}).get("com.docker.compose.service")
    services.add(service)
    policy = (host.get("RestartPolicy") or {}).get("Name")
    if policy != "unless-stopped":
        print(f"stack_smoke.sh: {name} has the restart policy {policy!r}, not unless-stopped",
              file=sys.stderr)
        bad += 1
    ports = {p: b for p, b in (host.get("PortBindings") or {}).items() if b}
    if service == "web":
        if ports != {"8110/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8110"}]}:
            shown = ", ".join(f"{x.get(HOST_IP)}:{x.get(HOST_PORT)}"
                              for b in ports.values() for x in b) or "nothing"
            print(f"stack_smoke.sh: {name} is published on {shown}, not 127.0.0.1:8110 alone",
                  file=sys.stderr)
            bad += 1
    elif ports:
        print(f"stack_smoke.sh: {name} has a port published", file=sys.stderr)
        bad += 1
if "web" not in services:
    print("stack_smoke.sh: no container of the stack is its web service", file=sys.stderr)
    bad += 1
sys.exit(1 if bad else 0)
' "${#CONTAINERS[@]}" || exit 1

STEP=teardown
# the stack counts as up until it is down, so a failed `down` is tried again by the trap
compose down -v --remove-orphans || exit 1
UP=0
STEP=""
exit 0
