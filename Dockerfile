# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
# tripbox worker image: Python 3.14 + uv-managed venv + kitinerary-extractor.
# Base images pinned by digest: python:3.14-slim and uv 0.13.5 as of 2026-10-26; each digest is a
# multi-platform index, so the same lines build for amd64 and arm64.
# Bump: `docker buildx imagetools inspect <image:tag> --format '{{.Manifest.Digest}}'`.
FROM python:3.14-slim@sha256:35caba83af2fe6da9a5d922f9b0156f21d1246a48278c58a08bd253303ba42e8 AS runtime
COPY --from=ghcr.io/astral-sh/uv:0.13.5@sha256:32a7bb39b496babbd997b0a39a4d66c126bdaf5661b302bf6f06abda8deca6ff /uv /usr/local/bin/uv

# kitinerary-extractor, from one of two sources:
#   kde     KDE's static CI build, sha256-pinned, newer extractors; published for x86_64 only.
#   debian  the base release's own libkitinerary-bin (24.12.3, older extractors), at any Debian
#           revision of it, so a security update still builds.
# auto (the default) is kde on amd64 and debian on any other architecture. Either way the one link
# /usr/local/bin/kitinerary-extractor is what the parser runs.
ARG KITINERARY_SOURCE=auto
ARG KITINERARY_VERSION=26.04.3
ARG KITINERARY_SHA256=ed2d1da43e2299a944f25b1521cb40183bbf9bc7691fa0f3d53a69c118dff778

RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    source="${KITINERARY_SOURCE}"; \
    if [ "$source" = auto ]; then \
      if [ "$arch" = amd64 ]; then source=kde; else source=debian; fi; \
    fi; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates tzdata; \
    case "$source" in \
      kde) \
        [ "$arch" = amd64 ] || { echo "KITINERARY_SOURCE=kde: KDE builds for amd64 only, not $arch" >&2; exit 1; }; \
        apt-get install -y --no-install-recommends curl; \
        curl -fsSLo /tmp/k.tgz "https://cdn.kde.org/ci-builds/pim/kitinerary/release-${KITINERARY_VERSION%.*}/linux/kitinerary-extractor-x86_64-${KITINERARY_VERSION}.tgz"; \
        echo "${KITINERARY_SHA256}  /tmp/k.tgz" | sha256sum -c -; \
        mkdir -p /opt/kitinerary; tar -xzf /tmp/k.tgz -C /opt/kitinerary; rm /tmp/k.tgz; \
        bin=/opt/kitinerary/bin/kitinerary-extractor ;; \
      debian) \
        apt-get install -y --no-install-recommends 'libkitinerary-bin=24.12.3-*'; \
        bin="$(dpkg -L libkitinerary-bin | grep '/kitinerary-extractor$')" ;; \
      *) echo "KITINERARY_SOURCE must be auto, kde or debian, not $source" >&2; exit 1 ;; \
    esac; \
    [ -x "$bin" ]; \
    ln -s "$bin" /usr/local/bin/kitinerary-extractor; \
    apt-get clean; rm -rf /var/lib/apt/lists/*

# The container runs as 1000:1000 unless compose's `user:` (or `docker run --user`) gives it another
# uid:gid; neither needs a passwd entry -> HOME=/tmp. It writes only to its mounted folders and /tmp.
# QT_QPA_PLATFORM/LANG keep kitinerary (a Qt binary) quiet and headless.
# UV_COMPILE_BYTECODE: the root filesystem is read-only at run time, so without .pyc files in
# the image every poll the worker starts recompiles ~2,600 modules (about 1.5 s).
ENV UV_PROJECT_ENVIRONMENT=/app/.venv UV_FROZEN=1 UV_NO_SYNC=1 UV_COMPILE_BYTECODE=1 \
    PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 HOME=/tmp LANG=C.UTF-8 QT_QPA_PLATFORM=offscreen TZ=UTC
# The image marks itself: `tripbox web --host 0.0.0.0` with no login is then the container's every
# interface, which compose publishes at TRIPBOX_LISTEN (kept on loopback with no login).
ENV TRIPBOX_IN_IMAGE=1
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src/ ./src/
COPY scripts/ ./scripts/
COPY config/ ./config/
RUN uv sync --frozen --no-dev && chmod +x scripts/healthcheck.py
COPY tests/corpus/ ./tests/corpus/
# The licence, the notice and what the third parties' licences ask a copy to carry
COPY LICENSE NOTICE THIRD_PARTY_NOTICES.md ./
COPY LICENSES/ ./LICENSES/
RUN chmod -R a+rX,go-w /app/LICENSE /app/NOTICE /app/THIRD_PARTY_NOTICES.md /app/LICENSES
# Any user reads the app and its corpus, and only its owner may change them, whatever the umask
# of the checkout it was built from.
RUN chmod -R a+rX,go-w /app/src /app/scripts /app/config /app/tests

# The worker is the image's command; a container given a command runs that instead. Healthy while
# the worker's heartbeat in the ledger is fresh.
USER 1000:1000
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD ["python", "/app/scripts/healthcheck.py", "worker"]
CMD ["python", "-m", "tripbox", "work"]

# Test stage: same image plus the dev group (pytest, ruff). Build with --target test. It stays
# root: the docker wrapper scripts' own tests need an account-database entry; the app suite is run as any user.
FROM runtime AS test
USER root
# git: the tool tests under tools/ run `git init` in throwaway repositories.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && apt-get clean && rm -rf /var/lib/apt/lists/*
COPY tests/ ./tests/
# the compose file and the example settings, for the tests that check them
COPY compose.yaml .env.example ./
COPY docs/configuration.md docs/gmail-filters.md ./docs/
RUN chmod -R a+rX,go-w /app/tests /app/docs /app/compose.yaml /app/.env.example
RUN uv sync --frozen
