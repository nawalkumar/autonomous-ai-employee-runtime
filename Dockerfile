FROM python:3.10-slim

# Run as a non-root user whose UID/GID match the host user that owns the
# bind-mounted ./data and ./workspace directories (see docker-compose.yml).
ARG APP_UID=1000
ARG APP_GID=1000

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:0.12.0 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

COPY pyproject.toml uv.lock README.md ./
COPY app ./app

RUN uv sync --frozen --no-dev --no-editable \
    && rm /usr/local/bin/uv

# The application code stays owned by root (read-only for the app user); only
# the data, workspace and public-demo directories are writable.
RUN groupadd --non-unique --gid "${APP_GID}" app \
    && useradd --non-unique --uid "${APP_UID}" --gid "${APP_GID}" \
       --no-create-home --shell /usr/sbin/nologin app \
    && mkdir -p /app/data /app/workspace /tmp/aae-public-demo \
    && chown app:app /app/data /app/workspace /tmp/aae-public-demo

USER app

# Inside the container the server must listen on all interfaces. Local mode
# still only answers requests addressed to localhost, and docker-compose
# publishes the port on 127.0.0.1 only. Hosting providers set PORT.
ENV APP_HOST=0.0.0.0 \
    APP_PORT=8000

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/api/v1/health' % (os.environ.get('PORT') or os.environ.get('APP_PORT', '8000')), timeout=4)"]

CMD ["aae-api"]
