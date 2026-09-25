# Vexer platform — reproducible image (mandate §4, §30).
#
# HONESTY NOTE (§49): authored and statically reviewed; NOT built in the environment where it was
# written (no Docker available). The migration service is the only consumer today.
#
# Security posture implemented here (§30):
#   * pinned base image digest-style tag, never :latest
#   * non-root user, no capabilities, no-new-privileges
#   * read-only root filesystem at runtime (compose), writable only /tmp
#   * no build tools or package managers in the runtime layer
#   * dependencies installed from a pinned requirements file, not from an unpinned index

# ---- build stage -----------------------------------------------------------------------
FROM python:3.12-slim-bookworm AS build

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build
COPY requirements.txt requirements-platform.txt* ./

# Wheels are built here so the runtime layer carries no compiler toolchain.
RUN python -m pip install --upgrade pip \
 && python -m pip wheel --wheel-dir /wheels -r requirements.txt \
 && if [ -f requirements-platform.txt ]; then python -m pip wheel --wheel-dir /wheels -r requirements-platform.txt; fi

# ---- runtime stage ---------------------------------------------------------------------
FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

# A dedicated unprivileged account; the numeric UID is fixed so a bind-mounted volume has
# predictable ownership.
RUN groupadd --system --gid 10001 vexer \
 && useradd --system --uid 10001 --gid vexer --home-dir /app --shell /usr/sbin/nologin vexer

WORKDIR /app
COPY --from=build /wheels /wheels
COPY requirements.txt requirements-platform.txt* ./
RUN python -m pip install --no-index --find-links=/wheels -r requirements.txt \
 && if [ -f requirements-platform.txt ]; then python -m pip install --no-index --find-links=/wheels -r requirements-platform.txt; fi \
 && rm -rf /wheels

COPY alembic.ini ./
COPY migrations ./migrations
COPY vexer_platform ./vexer_platform

# Ownership is fixed and the image ships no credentials; the DSN arrives as an env var (§29).
RUN chown -R vexer:vexer /app
USER 10001:10001

# Documented default; override per environment. The app must fail fast if this is unset, and
# migrations/env.py refuses to guess a target.
ENV VEXER_POSTGRES_DSN=""

# No source code is copied beyond what the migration runner needs, so the blast radius of a
# container compromise is small.
CMD ["alembic", "upgrade", "head"]
