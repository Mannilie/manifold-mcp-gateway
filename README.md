# Manifold

Self-hosted MCP gateway. One Docker image serves many MCP endpoints, each registered in claude.ai as its own connector.

Read `SPEC.md` for what it is and `DECISIONS.md` for why it is built the way it is.

## Endpoints

```
/                     admin UI (Phase 3)
/healthz              liveness
/<toolset>            MCP Streamable HTTP endpoint for that toolset
/<toolset>/healthz    toolset health
```

## Local development

Requires `uv` and `pnpm`. Python is pinned to 3.12 through `uv`.

```
cp .env.example .env            # then set a real MANIFOLD_MASTER_KEY
uv sync
uv run pytest
uv run ruff check . && uv run ruff format .
set -a; source .env; set +a; uv run python -m manifold
```

Or with Docker:

```
docker compose -f deploy/docker-compose.yml up --build
```

## Adding a native toolset

Create `manifold/toolsets/<name>/__init__.py` exposing `MANIFEST`, `build()` and `healthcheck()`. See `manifold/toolsets/manifold` for the shape. The contract tests in `tests/contract` pick it up automatically and it is served at `/<MANIFEST.key>` after a restart.

## Deployment

The image is published to `ghcr.io/mannilie/manifold` by GitHub Actions on every push to `main`. It runs as UID 99 GID 100 and keeps all state under `/data`. Port 8800 is never published on the host; cloudflared reaches the container over the Docker network. See `deploy/unraid/manifold.xml` and SPEC.md section 11.
