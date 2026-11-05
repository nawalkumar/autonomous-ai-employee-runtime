FROM python:3.10-slim

# Run as a non-root user whose UID/GID match the host user that owns the
# bind-mounted ./data and ./workspace directories (see docker-compose.yml).
ARG APP_UID=1000
ARG APP_GID=1000

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:0.12.0 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

COPY pyproject.toml uv.lock README.md ./
COPY app ./app

RUN uv sync --frozen --no-dev --no-editable

RUN groupadd --non-unique --gid "${APP_GID}" app \
    && useradd --non-unique --uid "${APP_UID}" --gid "${APP_GID}" \
       --no-create-home --shell /usr/sbin/nologin app \
    && mkdir -p /app/data /app/workspace \
    && chown app:app /app/data /app/workspace

USER app

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
