# --------- Builder Stage ---------
FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim AS builder

# Set environment variables for uv
ENV UV_COMPILE_BYTECODE=1
ENV UV_LINK_MODE=copy

WORKDIR /app

# Install dependencies first (for better layer caching)
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-install-project

# Copy the project source code
COPY . /app

# Install the project in non-editable mode
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-editable

# --------- Test Stage ---------
# The runtime image below deliberately carries only the main dependency set, so it can't
# run the suite. Rather than `pip install`ing pytest & friends at container start - which
# resolves whatever versions happen to be current that day, and needed root to do it -
# this stage installs the `dev` extra from the same `uv.lock` as everything else.
# Used by `docker-compose.test.yml`.
FROM builder AS test

ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONPATH=/code

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-editable --extra dev

WORKDIR /code

CMD ["pytest", "tests/", "-v"]

# --------- Final Stage ---------
FROM python:3.14-slim-bookworm

# Create a non-root user for security
RUN groupadd --gid 1000 app \
    && useradd --uid 1000 --gid app --shell /bin/bash --create-home app

# Copy the virtual environment from the builder stage
COPY --from=builder --chown=app:app /app/.venv /app/.venv

# Ensure the virtual environment is in the PATH
ENV PATH="/app/.venv/bin:$PATH"

# Where uploaded dive-computer exports and c-card images live (FILE_STORAGE_DIR). Created
# here, owned by `app`, and *before* `USER app` - which is load-bearing rather than tidy:
# the first time a named volume is mounted over an image directory, Docker copies that
# directory's contents **and its ownership** into the empty volume. That is the entire
# mechanism by which the uid-1000 process gets a writable volume with nothing for the
# operator to do. Without this line the volume is created root-owned, and the container
# fails its own startup writability check.
RUN mkdir -p /data/files && chown -R app:app /data

# Switch to the non-root user
USER app

# Set the working directory
WORKDIR /code

# The application package itself. Without this the image contains only the virtualenv and
# `/code` is empty, so `app.main:app` is unimportable and the container can only run when
# `docker-compose.yml` bind-mounts `./src/app` over `/code/app` - i.e. the image worked in
# local development and nowhere else. The compose bind mount still overlays this copy for
# live editing; it is now a convenience rather than a requirement.
COPY --from=builder --chown=app:app /app/src/app /code/app

# The migrations, and the config the `alembic` CLI needs to find them. The API runs
# `alembic upgrade head` on startup through a config built in code (`core.db.migrations`),
# which locates this directory relative to `/code/app` and needs no ini at all - but an
# operator running `alembic stamp head`, `alembic current` or `alembic downgrade` inside
# the container gets the CLI's usual `alembic.ini` behaviour, resolved from this WORKDIR.
COPY --from=builder --chown=app:app /app/src/migrations /code/migrations
COPY --from=builder --chown=app:app /app/src/alembic.ini /code/alembic.ini

EXPOSE 8000

# `python -c` rather than curl/wget: neither is installed in the slim base, and adding
# one just to health-check is a bigger surface than the check is worth.
#
# `/health/ready` rather than `/health`: this is the check `docker compose ps` reports and
# the one a `depends_on: condition: service_healthy` waits on, and a dependent asking that
# question wants "can serve", not "has a process". `/health` answers the latter and stays
# the probe for anything that restarts on failure. Nothing here restarts on unhealthy -
# Docker has no such policy - so a Postgres or Redis outage shows up as a red status
# rather than as a container being killed, which is the honest reading of it.
#
# A 503 raises `HTTPError` out of `urlopen` and exits non-zero on the traceback, which is
# the intended result; `docker inspect` shows the status line in the health log.
#
# This assumes the container serves HTTP, which only `api` does. Every other service built
# from this image - `worker` today - inherits the check and fails it forever, so it must
# override `healthcheck:` in `docker-compose.yml` with something it can actually pass, or
# disable it (`admin_init`).
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health/ready', timeout=4).status == 200 else 1)"]

# Multi-worker and no autoreload: this is the image that ships. `--reload` runs a single
# worker plus a filesystem watcher and restarts on any write, which is what you want
# while developing and never what you want in production - `docker-compose.yml`
# overrides `command:` with the uvicorn/--reload form for local work.
#
# Running the admin panel alongside this needs CRUD_ADMIN_DB_URL pointed at a shared
# database - see src/.env.example. Its schema setup is a one-shot (`admin_init` in
# docker-compose.yml), not something each worker does on boot.
CMD ["gunicorn", "app.main:app", "-w", "4", "-k", "uvicorn.workers.UvicornWorker", "-b", "0.0.0.0:8000"]
