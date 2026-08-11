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

EXPOSE 8000

# `python -c` rather than curl/wget: neither is installed in the slim base, and adding
# one just to health-check is a bigger surface than the check is worth.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health', timeout=4).status == 200 else 1)"]

# Multi-worker and no autoreload: this is the image that ships. `--reload` runs a single
# worker plus a filesystem watcher and restarts on any write, which is what you want
# while developing and never what you want in production - `docker-compose.yml`
# overrides `command:` with the uvicorn/--reload form for local work.
#
# NOTE: incompatible with CRUD_ADMIN_ENABLED=true. Every worker runs the lifespan, so every
# worker calls `admin.initialize()` against the same SQLite file and they race; the
# container then fails to boot. The panel defaults to off, so this only bites if you turn
# it on - in which case run it as a separate single-worker instance. See DECISIONS.md.
CMD ["gunicorn", "app.main:app", "-w", "4", "-k", "uvicorn.workers.UvicornWorker", "-b", "0.0.0.0:8000"]
