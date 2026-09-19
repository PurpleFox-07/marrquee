# syntax=docker/dockerfile:1
#
# Two stages: the first has uv and a full Python toolchain and builds a
# virtual environment; the second copies only that finished environment out,
# so nothing that depends on the repository being on disk (a template path,
# a tests/ import) can accidentally survive into the shipped image.

FROM python:3.12-slim-bookworm AS builder

# uv arrives as a static binary lifted from its own official image, not a
# `pip install uv` step - that would pull uv (and pip) into a layer this
# stage throws away anyway. https://docs.astral.sh/uv/guides/integration/docker/
COPY --from=ghcr.io/astral-sh/uv:0.12.17 /uv /bin/uv

# UV_LINK_MODE=copy avoids a hardlink warning across the cache-mount and
# build-context filesystems. UV_PYTHON_DOWNLOADS=never forces uv onto this
# base image's own Python 3.12 instead of fetching a second interpreter,
# which would both bloat the image and vary by CPU architecture.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Install dependencies in their own layer, from just the lockfile and
# project metadata. This layer is invalidated only when a dependency
# changes, not every time a template or a line of Python does - which
# matters most for the arm64 build, since that one runs under QEMU.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-install-project --no-dev

# Now bring in the source and install the project itself, non-editable: the
# runtime stage below copies only this stage's /app/.venv, so an editable
# install (which points back at /app/src instead of copying into the venv)
# would ship a virtual environment with nothing behind its own symlinks.
COPY . /app
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

FROM python:3.12-slim-bookworm

# No USER directive: this container's whole job is reading the host's
# /var/run/docker.sock, which ships root:docker at mode 660. A non-root user
# would need that group's numeric GID, which differs on every NAS; running
# as root removes that per-install failure mode without adding new exposure,
# since mounting the socket at all is already root-equivalent access.
COPY --from=builder /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH" \
    MARRQUEE_CONFIG_DIR=/config \
    MARRQUEE_DOCKER_SOCKET=/var/run/docker.sock \
    MARRQUEE_PORT=7788 \
    PYTHONUNBUFFERED=1

EXPOSE 7788
VOLUME /config

# python:*-slim carries neither curl nor wget, and installing one just for a
# health check would be a dependency for nothing stdlib can't already do.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('MARRQUEE_PORT', '7788') + '/healthz', timeout=3)"]

CMD ["python", "-m", "marrquee"]
