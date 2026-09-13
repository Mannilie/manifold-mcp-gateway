# CLAUDE.md

Manifold is a self-hosted MCP gateway. Read `SPEC.md` in full before doing anything. Read `DECISIONS.md` if it exists so you do not re-ask settled questions.

## Who you are working with

Manny is an engineering manager and experienced engineer. Give direct feedback, no sugarcoating, no filler. Answer first, then detail. UK/Australian English spelling in all code comments, docs, UI copy and commit messages.

## Decision gates

At the start of each phase, and before any choice listed in SPEC.md section 15 or any other choice that is expensive to reverse (schema, storage, auth, URL structure, framework, library with a large surface area):

1. Present 2 or 3 options in a table: what it is, what it trades off, what it costs to change later.
2. Give a recommendation with one sentence of reasoning.
3. Stop. Wait for Manny to pick. Do not proceed on your own recommendation without a reply.
4. After Manny picks, append the decision to `DECISIONS.md` with date, options, choice and reason, then continue.

If you are unsure whether something needs a gate, it does.

## Phases

Work strictly in the phases defined in SPEC.md section 14. Do not start a phase until the previous phase's completion criterion is confirmed by Manny. At the end of each phase, produce a short summary of what was built, what Manny needs to do manually (Cloudflare, Unraid template, claude.ai connector), and what the next phase will ask.

## Stack

- Python 3.12, FastAPI, official `mcp` SDK 2.x (`MCPServer`, formerly FastMCP), SQLite via `aiosqlite`, `httpx2` (the SDK's HTTP client; do not add `httpx`)
- Astro for the admin UI, static build, TypeScript, no UI framework unless a gate decides otherwise
- `uv` for Python dependency management, `pnpm` for the UI
- `pytest` with `pytest-asyncio`, `ruff` for lint and format
- Multi-stage Dockerfile, final image runs as UID 99 GID 100

## Repo layout

```
manifold/
  app.py                 ASGI entry, mounts UI, API, toolsets
  config/                settings, env loading
  store/                 SQLite schema, migrations, repositories
  crypto/                credential encryption
  auth/                  Cloudflare header check, OAuth flows
  gateway/               toolset registry, mounting, proxy client
  toolsets/
    manifold/            self-management, always on
    sheets/
  api/                   admin API routers
ui/                      Astro project
tests/
  contract/              every toolset must pass these
  unit/
  integration/
deploy/
  unraid/manifold.xml
  docker-compose.yml
SPEC.md
CLAUDE.md
DECISIONS.md
README.md
```

## Conventions

- Toolset keys are permanent. Never rename one without a gate.
- Every native toolset ships with `MANIFEST`, `build()`, `healthcheck()` and passes `tests/contract`.
- Every tool has a docstring that becomes its MCP description. Write it for Claude as the reader: what it does, what it returns, when not to use it.
- Never log, print, return in an API response, or include in an export any credential value. Tests must assert this for every new credential path.
- Audit log stores an args hash, never raw args.
- All paths inside the container under `/data` for persistent state. Nothing persistent elsewhere.
- Config lives in SQLite. There is no config file to edit at runtime. The only env vars are those listed in SPEC.md section 11.
- UI copy is short and plain. No exclamation marks.
- Commit after each meaningful unit of work with a conventional commit message. Do not squash phases into one commit.

## Commands

```
uv run pytest                  run all tests
uv run ruff check . && uv run ruff format .
pnpm --dir ui build            build the admin UI
pnpm --dir ui check            typecheck the admin UI
docker compose -f deploy/docker-compose.yml up --build
```

Keep these accurate. If you change a command, update this file in the same commit.

## What Manny does manually

Claude Code does not touch: Cloudflare dashboard, Unraid Docker UI, claude.ai connector setup, Google Cloud Console. Produce clear step lists for these and stop.

## Do not

- Do not add a framework, ORM, or major dependency without a gate.
- Do not implement stdio upstream proxying in v1.
- Do not build multi-user features.
- Do not proceed past a decision gate on your own recommendation.
- Do not write secrets, spreadsheet IDs, or real upstream URLs into any committed file. Use `.env.example` and `manifold.example.yaml` placeholders.
