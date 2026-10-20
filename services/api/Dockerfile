# syntax=docker/dockerfile:1.7
#
# Production image for services/api (FastAPI; the worker shares this codebase).
#
# The build context is the WORKSPACE ROOT, not services/api: this is a uv
# workspace and `uv.lock` + the root `pyproject.toml` live there. Build with
#   docker build -f services/api/Dockerfile -t codeaxis-api .
# from the repo root. `fly.toml` sits at the root for the same reason.
#
# Security posture is documented in infra/sandbox/README.md and asserted by
# infra/sandbox/tests/test_sandbox_env.py, which parses this file. Changing a
# USER, apt-get or uv line here will fail that test until the contract is
# updated deliberately.

# --------------------------------------------------------------- builder
#
# Exists to keep uv, its cache and the package metadata out of the runtime
# image. It deliberately installs NO compiler: every native dependency in
# uv.lock (tree-sitter, tree-sitter-language-pack, python-igraph, leidenalg,
# psycopg-binary, lxml) resolves to a manylinux wheel for both x86_64 and
# aarch64, so a toolchain would only be dead weight and a thing an analyzed
# repository must never find. If a future dependency has no wheel the build
# fails loudly here rather than quietly growing a gcc.
FROM python:3.12-slim-bookworm AS builder

# Pinned by digest-bearing tag rather than `latest`: the lockfile is only
# authoritative if the thing reading it does not change under us.
COPY --from=ghcr.io/astral-sh/uv:0.13.1 /uv /usr/local/bin/uv

# UV_PYTHON_DOWNLOADS=never forces the image's own CPython 3.12, which is what
# uv.lock pins (requires-python == "3.12.*"); a downloaded interpreter would
# make the runtime stage depend on files we do not copy.
ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app

# Dependency manifests only, so the expensive sync layer survives a source
# edit. The root project is `package = false`; codeaxis-api is the member.
COPY pyproject.toml uv.lock ./
COPY services/api/pyproject.toml services/api/pyproject.toml

# --frozen: resolve nothing, fail if uv.lock disagrees with the manifests.
# --no-dev: pytest/ruff/pyright are not shipped.
# --no-install-workspace: third-party deps now, our own code in the next layer.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-workspace --package codeaxis-api

COPY services/api/app services/api/app

# Installs codeaxis-api itself. uv installs a workspace member editable, so the
# runtime stage must keep the source at this exact path (/app/services/api).
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --package codeaxis-api

# --------------------------------------------------------------- runtime
FROM python:3.12-slim-bookworm AS runtime

# git is the only toolchain this image gets: app/repository/ingest.py and
# app/analysis/scan/history.py shell out to it. Explicitly NOT installed, so an
# analyzed repository cannot find them: any compiler, make, node/npm, python
# build backends, git-lfs (ingest sets GIT_LFS_SKIP_SMUDGE=1 regardless), or a
# shell beyond the base image's /bin/sh and /bin/bash.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates git \
 && rm -rf /var/lib/apt/lists/*

# Strip every setuid/setgid bit in the image. The process runs as uid 10001 and
# reads untrusted repository content; with no setuid binary left there is no
# in-image path back to uid 0 even if something is tricked into exec'ing one.
RUN find / -xdev -type f -perm /6000 -exec chmod a-s {} + 2>/dev/null || true

RUN groupadd --system --gid 10001 codeaxis \
 && useradd --system --uid 10001 --gid 10001 \
      --home-dir /home/codeaxis --create-home --shell /usr/sbin/nologin codeaxis

# Both stay root-owned and mode 0755: the application cannot rewrite its own
# code or its dependencies, which is what makes a read-only rootfs meaningful
# rather than decorative.
COPY --from=builder --chown=root:root /app/.venv /app/.venv
COPY --chown=root:root services/api/app /app/services/api/app

# PYTHONDONTWRITEBYTECODE: the builder already compiled bytecode
# (UV_COMPILE_BYTECODE=1), and the rootfs is read-only wherever the platform
# supports it (`docker run --read-only`; a Fly machine gets a writable
# ephemeral rootfs inside a microVM instead), so attempted .pyc writes are at
# best noise and at worst a write into code we want immutable.
# PYTHONUNBUFFERED: app/logging.py logs structured lines to stdout and Fly/
# Docker reads them from there. There is no log file in this image by design.
# TMPDIR: named explicitly because ingest.is_scratch_checkout only recognises a
# scratch checkout as a direct child of tempfile.gettempdir() -- mount the
# writable scratch volume at /tmp and nowhere else.
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONFAULTHANDLER=1 \
    TMPDIR=/tmp \
    HOME=/home/codeaxis

WORKDIR /app/services/api

USER 10001:10001

EXPOSE 8080

# Single worker on purpose. Scan jobs run in-process via FastAPI
# BackgroundTasks (app/analysis/jobs.py's STUB note), so a worker's memory
# footprint is a whole tree-sitter + igraph + leidenalg scan; N workers means N
# of those against one memory limit. Scale by adding machines, not workers.
#
# --timeout-graceful-shutdown keeps a machine alive after SIGTERM while an ASGI
# call is still running. A BackgroundTasks task runs inside that call, so this
# is the one knob that stops a deploy from silently killing a running scan. It
# is an upper bound only: the platform's own kill timeout wins if it is shorter
# (Fly caps kill_timeout at 300s -- see fly.toml and docs/deployment.md).
#
# --no-access-log because app/logging.py's RequestLoggingMiddleware already
# emits exactly one line per request; uvicorn's would duplicate it with a
# different field order. --proxy-headers/--forwarded-allow-ips trust the
# platform edge proxy for the client IP; the API is only ever reached through
# it, never directly.
ENTRYPOINT ["uvicorn", "app.main:app", \
            "--host", "0.0.0.0", \
            "--port", "8080", \
            "--workers", "1", \
            "--proxy-headers", \
            "--forwarded-allow-ips", "*", \
            "--timeout-graceful-shutdown", "3600", \
            "--no-server-header", \
            "--no-access-log"]
