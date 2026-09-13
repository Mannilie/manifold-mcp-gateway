# Manifold

Self-hosted MCP gateway. One Docker image serves many MCP endpoints, each registered in claude.ai as its own connector.

Read `SPEC.md` for what it is and `DECISIONS.md` for why it is built the way it is.

## Endpoints

```
/                     admin UI (Phase 3)
/healthz              liveness
/<toolset>            MCP Streamable HTTP endpoint, bearer token required
/<toolset>/healthz    toolset health, open
/oauth/authorize      consent step, behind Cloudflare Access
/oauth/token, /oauth/register, /oauth/revoke
/.well-known/oauth-authorization-server
/.well-known/oauth-protected-resource/<toolset>
```

claude.ai is given `https://<host>/<toolset>` as a custom connector. It discovers the OAuth server from the 401, registers itself and runs the code flow. The only human login is Cloudflare Access on the authorize page. OAuth clients and tokens live in SQLite under `/data`, so restarts and updates do not disconnect claude.ai.

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

## Admin UI

`ui/` is an Astro static build with a small vanilla TypeScript app. Build it with `pnpm --dir ui build`; the Dockerfile does the same in a Node stage. The app talks to `/api` only. Mutating requests carry `X-Manifold-Request: 1`, which the API requires.

To work on the UI locally you need a request with a Cloudflare Access identity. Run the app behind a tiny wrapper that injects the `Cf-Access-Authenticated-User-Email` header, never expose that wrapper anywhere.

New native toolsets are registered disabled. Enable them from the dashboard, or in an emergency from the database on the NAS:

```
docker exec manifold python -c "import sqlite3; c=sqlite3.connect('/data/manifold.db'); c.execute(\"UPDATE toolsets SET enabled=1 WHERE key='sheets'\"); c.commit()"
```

## Live Google test

`tests/live` runs against a real spreadsheet and skips itself in CI. Point it at a service account key and a throwaway spreadsheet shared with that account as Editor:

```
MANIFOLD_TEST_SA_JSON=/path/to/sa.json MANIFOLD_TEST_SPREADSHEET_ID=1abc... uv run pytest tests/live -q
```

## Toolsets in the image

- `manifold`: gateway self-management, always on.
- `sheets`: Google Sheets by spreadsheet ID, service account or Google OAuth.
- `unraid`: the NAS through the Unraid 7.2 API with an api_key credential.

Proxy toolsets, such as `n8n`, are rows in the config store created from the admin UI.

## Adding a native toolset

Create `manifold/toolsets/<name>/__init__.py` exposing `MANIFEST`, `build()` and `healthcheck()`. See `manifold/toolsets/manifold` for the shape. The contract tests in `tests/contract` pick it up automatically and it is served at `/<MANIFEST.key>` after a restart.

## Backups, restore and key rotation

Snapshots land in `/data/backups` daily, before every migration, before every restore and
before a key rotation, newest 14 kept. Settings lists them for download, takes one on demand,
and restores in two steps with a comparison against the live database. Key rotation is
`python -m manifold rotate-key`. Step lists are in `docs/runbooks.md`.

## Deployment

The image is published to `ghcr.io/mannilie/manifold` by GitHub Actions on every push to `main`. It runs as UID 99 GID 100 and keeps all state under `/data`. Port 8800 is bound to the NAS loopback only; cloudflared runs with host networking and reaches it at `localhost:8800`. It is never exposed on a LAN interface. See `deploy/unraid/manifold.xml` and SPEC.md section 11.
