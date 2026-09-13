# syntax=docker/dockerfile:1.7

FROM node:22-alpine AS ui
WORKDIR /ui
RUN corepack enable
COPY ui/package.json ui/pnpm-lock.yaml ui/pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile
COPY ui/ ./
RUN pnpm check && pnpm build

FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project
COPY manifold ./manifold
COPY --from=ui /ui/dist ./manifold/ui_dist
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable

FROM python:3.12-slim-bookworm
# UID 99 GID 100 are baked in. There is no root entrypoint and no PUID/PGID handling.
RUN useradd --uid 99 --gid 100 --no-create-home --shell /usr/sbin/nologin manifold \
    && mkdir -p /data && chown 99:100 /data
COPY --from=builder --chown=99:100 /app/.venv /app/.venv
WORKDIR /app
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    MANIFOLD_DATA_DIR=/data
USER 99:100
VOLUME ["/data"]
EXPOSE 8800
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8800/healthz', timeout=3).status == 200 else 1)"
CMD ["python", "-m", "manifold"]
