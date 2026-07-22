FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim@sha256:e5b65587bce7de595f299855d7385fe7fca39b8a74baa261ba1b7147afa78e58 AS builder
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-install-project --no-dev
COPY . .
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev

FROM python:3.12-slim-bookworm@sha256:d50fb7611f86d04a3b0471b46d7557818d88983fc3136726336b2a4c657aa30b
RUN apt-get update \
    && apt-get install --no-install-recommends --yes ca-certificates git \
    && rm -rf /var/lib/apt/lists/*
RUN useradd --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin --uid 10001 repopilot
WORKDIR /app
COPY --from=builder --chown=root:root /app /app
RUN mkdir -p /app/data /repositories /workspace \
    && chown repopilot:repopilot /app/data \
    && chmod 0750 /app/data \
    && chown repopilot:repopilot /repositories \
    && chmod 0750 /repositories \
    && chmod 0555 /workspace
USER repopilot
ENV HOME="/tmp" \
    PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    REPOPILOT_DATABASE_URL="sqlite+aiosqlite:////app/data/repopilot.db" \
    REPOPILOT_WORKSPACE_ROOT="/workspace" \
    REPOPILOT_REPOSITORY_ROOT="/repositories" \
    REPOPILOT_ALLOWED_REPOSITORY_ROOTS="/workspace,/imports"
VOLUME ["/app/data"]
VOLUME ["/repositories"]
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s --retries=3 \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/ready')"
CMD ["repopilot", "serve", "--host", "0.0.0.0", "--port", "8000"]
