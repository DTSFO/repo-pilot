FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-install-project --no-dev
COPY . .
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev

FROM python:3.12-slim-bookworm
RUN useradd --create-home --uid 1000 repopilot
WORKDIR /app
COPY --from=builder --chown=repopilot:repopilot /app /app
RUN mkdir -p /app/data /workspace && chown repopilot:repopilot /app/data /workspace
USER repopilot
ENV PATH="/app/.venv/bin:$PATH" \
    REPOPILOT_DATABASE_URL="sqlite+aiosqlite:////app/data/repopilot.db" \
    REPOPILOT_WORKSPACE_ROOT="/workspace"
VOLUME ["/app/data", "/workspace"]
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s --retries=3 \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/ready')"
CMD ["repopilot", "serve", "--host", "0.0.0.0", "--port", "8000"]
