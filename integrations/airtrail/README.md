# The Trips button for AirTrail

AirTrail's dock (the row of buttons at the foot of its map) has no way to tripbox. This folder
adds one: a **Trips** button, after Statistics, that opens tripbox's trips page. It is a link and
nothing more: AirTrail learns nothing of tripbox, and tripbox works the same with or without it.
AirTrail does not take plugins, so the button is a small patch to AirTrail's source, and you run
an AirTrail image you build yourself.

| File | What it is |
|---|---|
| `trips-dock.patch` | The patch: it adds nine lines to two of AirTrail's files and changes none |
| `build.sh` | Clones AirTrail at a release, applies the patch with your trips address, builds the image |
| `patch-check.sh` | Says whether the patch still applies to AirTrail's newest release |

## Licence

The patch is a modification of AirTrail, copyright its authors, and is offered under AirTrail's
own licence, the GNU General Public License version 3 (its first line says
`SPDX-License-Identifier: GPL-3.0-only`). An image built with it is AirTrail, modified: what the
GPL asks of anyone who passes AirTrail on applies to it. The rest of tripbox is under the GNU
Affero General Public License version 3.

## Building it

The patch was written for **AirTrail v3.14.0**; `patch-check.sh --tag <release>` says whether it
applies to another release. `build.sh` needs bash, git, docker and the network (it fetches
AirTrail and the packages AirTrail installs). Always build the release you run now:

> Never build an older release than the one you run: AirTrail upgrades its database on start
> and cannot go back.

1. **Back up AirTrail's database.** AirTrail upgrades its database on every start, and its
   upgrades run forward only: going back to the image you run now means restoring this backup.
   AirTrail's own guide says how (Administration → Backup and restore).
2. **Build the image**, naming the AirTrail release you run (such as `v3.14.0`: the first
   number in the foot of AirTrail's Settings window, with a `v` before it; your compose file's
   `image:` line may say only `latest`), where the button leads, and a tag for the image:

   ```
   integrations/airtrail/build.sh <the AirTrail release you run> https://trips.example.com \
     airtrail-trips:<that release>
   ```

   The address is tripbox's `TRIPBOX_BASE_URL`: `https://trips.example.com` when tripbox has a
   name of its own, or a path such as `/tripbox` when it is served at AirTrail's own hostname
   (`docs/reverse-proxy.md`, "AirTrail and tripbox at one hostname"). The last line is
   `build: ok airtrail-trips:<that release>`, or `build: FAIL <step>` (clone, patch, bases or
   image).
3. **Use it**: in AirTrail's compose file, change the `image:` line of AirTrail's own service
   (not its database's) to the tag you built, then `docker compose up -d`.

To check what you built serves the button, open AirTrail and look for **Trips** in the dock, or
ask its scripts (Python 3, nothing else):

```
python3 tools/verify/onehost/served.py --url https://flights.example.com --trips /tripbox
```

which ends `dock link: found` or `dock link: not found`.

## The way back

Put the `image:` line back as it was (`image: johly/airtrail:<the release you ran before>`) and run
`docker compose up -d`. If the patched image ran a newer AirTrail than the one you go back to,
restore the backup from step 1 first.

## Building from pinned images

AirTrail's Dockerfile names some of its base images by tag alone, so two builds a week apart may
start from different images. To build from images you have pinned, write a file of lines
`<image as the Dockerfile writes it> <image to build from>` and name it in
`AIRTRAIL_BASE_IMAGES`:

```
oven/bun:debian oven/bun:debian@sha256:<digest>
node:22-slim node:22-slim@sha256:<digest>
```

Each line replaces that image on every `FROM` line that names it; a line that matches no `FROM`
line stops the build at `bases`. `AIRTRAIL_REPO` clones another copy of AirTrail's repository,
and `AIRTRAIL_BUILD_DIR` keeps the clone in a folder of yours (absent or empty) instead of a
temporary one.

## A new AirTrail release

The patch adds its lines beside lines of AirTrail's that may change in a later release. When they
do, `build.sh` stops at `patch` (`build: the patch does not apply to AirTrail <tag>; it was
written for v3.14.0`) and builds nothing. `patch-check.sh` says the same for AirTrail's newest
release (`patch check: <tag> applies` or `patch check: <tag> FAILS`); tripbox's repository runs
it every week and opens an issue when the patch no longer applies.
