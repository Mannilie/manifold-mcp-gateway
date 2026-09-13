# Manifold: Specification

Version 0.1, September 2026. Owner: Manny (Mannilie).

## 1. Purpose

Manifold is a self-hosted MCP gateway that runs as a single container on manny-nas and exposes multiple independent MCP endpoints, each registered in claude.ai as its own connector. It hosts both native toolsets (custom tools written in this repo) and proxy toolsets (upstream MCP servers re-exported with a prefix), and provides a web UI for configuring toolsets, credentials, OAuth flows and gateway settings.

It replaces the pattern of one repo, one image and one connector per tool (as with House Hunt) with one repo, one image and many connectors.

## 2. Goals

- One Docker image, one container, one CI pipeline, one Watchtower target.
- Each toolset served at its own path and connected to claude.ai as a separate connector, so tool counts per connector stay small and toolsets can be toggled per chat.
- Adding a new native toolset is a matter of adding one Python package and restarting.
- Adding a proxy toolset is done entirely in the UI.
- All configuration and credentials managed through the web UI, encrypted at rest, with no need to edit files on the NAS after first deploy.
- Manifold is the OAuth 2.1 authorization server for claude.ai, the same shape as House Hunt. The only place a human logs in is Cloudflare Access on the authorize page. Manifold never stores a password.

## 3. Non-goals

- Not a general MCP marketplace or multi-tenant service. Single user.
- No public unauthenticated endpoints.
- No stdio transport for upstream proxies in v1 (HTTP upstreams only).
- No horizontal scaling. One process.
- No mobile-specific UI. Desktop browser first, usable on phone.

## 4. URL structure

Base domain: `mcp.mannylab.cloud`, via Cloudflare Tunnel.

```
/                         Admin UI (Cloudflare Access: Manny's email only)
/api/...                  Admin API consumed by the UI (same Access app as /)
/healthz                  Liveness, no auth, JSON
/<toolset>                MCP Streamable HTTP endpoint for that toolset
/<toolset>/healthz        Toolset health, no auth, JSON
/oauth/callback           OAuth redirect target for upstream providers (Phase 3)
/oauth/authorize          claude.ai consent step, behind Cloudflare Access
/oauth/token              token endpoint, no Access
/oauth/register           dynamic client registration, no Access
/oauth/revoke             token revocation, no Access
/.well-known/oauth-authorization-server            issuer metadata, no Access
/.well-known/oauth-protected-resource/<toolset>    per-toolset resource metadata, no Access
```

Toolset keys are lowercase, `[a-z0-9-]+`, and are permanent once a connector is registered. Renaming a key changes the endpoint URL and breaks the connector. The UI must warn on rename.

Reserved keys, rejected by the UI and the API: `api`, `healthz`, `oauth`, `assets`, `_astro`, `static`. They collide with paths the gateway already serves. Requests under the three asset prefixes go to the UI; the other three answer 404 if nothing else served them.

The MCP endpoint is at `/<toolset>` with no `/mcp` suffix. FastMCP's default streamable path must be overridden to `/` when mounting. A POST to `/<toolset>` must be handled directly, never redirected to `/<toolset>/`.

`https://mcp.mannylab.cloud/<toolset>` is exactly what gets pasted into claude.ai as a custom connector. claude.ai receives a 401 with a `WWW-Authenticate` hint, reads the two well-known documents, registers itself as a client and runs the authorization code flow with PKCE against Manifold.

## 5. Architecture

```
claude.ai connector "Manifold: Sheets"
        |
        v  (OAuth 2.1 against Manifold; the authorize page is behind Cloudflare Access)
Cloudflare Tunnel -> cloudflared (host network) -> 127.0.0.1:8800 (loopback only, never a LAN interface)
        |
        +-- /            Astro admin UI (static build served by FastAPI)
        +-- /api         FastAPI admin API
        +-- /sheets      FastMCP sub-app (native toolset)
        +-- /unraid      FastMCP sub-app (proxy -> unraid-mcp:6970)
        +-- /n8n         FastMCP sub-app (proxy -> n8n:5678)
        |
        +-- SQLite (config, credentials, audit)  /data (host: /mnt/user/appdata/manifold)
```

Process model: single ASGI app (FastAPI) with FastMCP server instances mounted per enabled toolset. Toolset registry rebuilt on config change without a full process restart where possible; full restart acceptable in v1 if hot-mount proves fragile (decision gate).

## 6. Toolset contract

Every toolset, native or proxy, is described by a manifest:

```python
class ToolsetManifest:
    key: str                     # URL path and prefix, permanent
    display_name: str
    description: str
    kind: Literal["native", "proxy"]
    supported_auth: list[AuthKind]   # see section 7
    settings_schema: JSONSchema      # rendered as a form in the UI
    example_settings: dict           # valid against settings_schema, used by the contract test
    version: str
```

Native toolsets live in `manifold/toolsets/<key>/` and expose:

```python
MANIFEST: ToolsetManifest
def build(config: ToolsetConfig, credentials: Credentials) -> FastMCP
async def healthcheck(config, credentials) -> HealthResult
```

`build()` returns a FastMCP instance with tools registered. Tool names are registered without prefix; the gateway does not rename tools within a toolset since each toolset is its own connector. Prefixing only applies when a proxy toolset re-exports upstream tools and a `prefix` is set.

Proxy toolsets are not code. They are rows in the config store with: key, display name, upstream URL, upstream auth (kind + credential ref), optional tool prefix, allow list, deny list.

Contract test: every native toolset must pass `tests/contract/test_toolset.py`, which checks manifest validity, that `example_settings` validates against `settings_schema`, that `build()` succeeds with `example_settings`, that every tool has a description, and that `healthcheck()` returns within 5 seconds.

A native toolset discovered on disk but not yet in the config store is registered disabled. `manifold` is the only exception: it is always enabled and cannot be disabled or deleted.

## 7. Credentials and auth kinds

```python
AuthKind = Literal["none", "api_key", "basic", "bearer", "service_account", "oauth2"]
```

| Kind | UI form | Stored |
|---|---|---|
| none | nothing | nothing |
| api_key | one masked field, header name | key |
| basic | username, masked password | both |
| bearer | one masked field | token |
| service_account | JSON upload; UI displays client_email after upload | full JSON |
| oauth2 | client ID, masked client secret, scopes, provider preset, Connect button | client creds, refresh token, access token + expiry |

Credentials are first-class objects with a name, shared between toolsets. A toolset declares which kinds it supports and the UI offers a picker of existing credentials of those kinds, plus "Add new". One Google credential named "Google (Manny)" can serve drive, docs and sheets. Deleting a credential that is in use is refused, and the refusal lists the toolsets using it. Changing a toolset to a credential of a different kind is allowed.

OAuth2 provider presets in v1: Google, Microsoft, generic (manual auth/token URLs). The consent flow runs in the browser from the admin UI, redirects to `/oauth/callback`, and stores the refresh token. The granted scope string from the token response is stored alongside the requested scopes and both are shown. Editing scopes marks the credential "reconnect required" and disables toolsets using it until reconnected. Token refresh is automatic, transparent to tools, and coalesced per credential. A refresh failure with `invalid_grant` sets dependent toolsets to degraded with a reconnect action; a transient upstream error does not. Google scopes: `spreadsheets` only in Phase 4, with `documents` and `drive.readonly` added when those toolsets land (DECISIONS.md, Phase 3 gate 4).

## 8. Config store

SQLite at `$MANIFOLD_DATA_DIR/manifold.db`, which is `/data/manifold.db` inside the container. Tables (indicative):

- `toolsets`: key, display_name, kind, enabled, credential_id (nullable FK), settings_json, created_at, updated_at
- `oauth_clients`: client_id, metadata_json, created_at (dynamic registrations from claude.ai)
- `oauth_tokens`: token_hash, kind (access, refresh, code), client_id, subject, resource, scopes_json, expires_at, partner_hash
- `gateway_settings`: key, value_json (log level, audit retention days, and other runtime settings from the Settings page)
- `credentials`: id, name, auth_kind, scheme, nonce, ciphertext, created_at, updated_at
- `key_check`: one row encrypted under the derived credentials key, verified at boot
- `proxy_upstreams`: toolset_key, upstream_url, prefix, allow_json, deny_json
- `audit_log`: id, ts, toolset_key, tool_name, args_hash, duration_ms, ok, error
- `oauth_state`: state, toolset_key, created_at (short-lived)

Encryption: AES-256-GCM with a key derived from `MANIFOLD_MASTER_KEY` by HKDF-SHA256 per purpose (DECISIONS.md, Phase 2 gate 1). Associated data binds each ciphertext to its scheme, credential id and auth kind. Loss of the master key means loss of all credentials; the UI must display this warning on first run.

Migrations: numbered SQL files applied at boot, tracked by `PRAGMA user_version`. The database file is copied aside before each migration and the last five copies are kept.

Export: the UI offers "Export config as YAML" (credentials redacted) for backup and diffing. Import is out of scope for v1.

## 9. Admin UI

Stack: Astro, built to static files at image build time, served by FastAPI. No SSR in v1 unless the decision gate lands there.

Pages:

1. **Dashboard**: toolset cards with enabled toggle, health dot (ok / degraded / down / disabled), endpoint URL with copy button, tool count, last call time, and a Cloudflare checklist: the exact Access bypass path the toolset needs (`/<toolset>`, which also covers `/<toolset>/healthz`) and the connector URL to paste into claude.ai, so adding a toolset comes with its manual steps attached.
2. **Toolset detail**: settings form generated from `settings_schema`, credential section per section 7, "Test connection" button, tool list with per-tool enable/disable, danger zone (rename with warning, delete).
3. **Add proxy toolset**: key, display name, upstream URL, auth, prefix, then a "Discover tools" step that lists upstream tools for allow/deny selection.
4. **Audit log**: paginated, filterable by toolset, tool, ok/error, time range.
5. **Settings**: master key status, export config, log level, audit retention days, restart button, and "Disconnect all connectors", which clears every OAuth client and token so each claude.ai connector must re-authorise.

Log level: `MANIFOLD_LOG_LEVEL` is the boot default. Once the config store is loaded, the value in `gateway_settings` overrides it if present.

The UI calls `/api/*` only. `/api` is protected by the same Cloudflare Access app as `/`. Manifold additionally checks the `Cf-Access-Authenticated-User-Email` header against `MANIFOLD_ADMIN_EMAILS` on `/api` (Phase 3) and on `/oauth/authorize` (Phase 1) as defence in depth, so a misconfigured Access policy does not expose the admin API or let a stranger authorise a connector. `/<toolset>` endpoints are protected by Manifold-issued bearer tokens instead, because claude.ai cannot pass an Access login.

## 10. Initial toolsets

### 10.1 `sheets` (native)

Google Sheets read and write. Supported auth: `service_account`, `oauth2` (Google preset). Settings: optional allow list of spreadsheet IDs (empty means any the credential can reach).

Tools:
- `list_sheets(spreadsheet_id)`
- `read_range(spreadsheet_id, range)`
- `append_rows(spreadsheet_id, sheet, rows)`
- `update_range(spreadsheet_id, range, values)`
- `find_rows(spreadsheet_id, sheet, column, value)`
- `update_rows_by_key(spreadsheet_id, sheet, key_column, updates)`
- `clear_range(spreadsheet_id, range)`
- `batch_update(spreadsheet_id, requests)` escape hatch to the raw Sheets API for formatting, formulas, structure changes

### 10.2 `unraid` (native or proxy, Phase 5 gate)

There is no existing Unraid MCP server on the NAS and none in Community Applications; the candidates are GitHub projects. The Phase 5 gate presents two options: a native toolset against the Unraid 7.2+ GraphQL API with an `api_key` credential, or one of the GitHub servers (`better-unraid-mcp`, `jmagar/unraid-mcp`) run from a hand-written template as a proxy upstream. Either way the tool list is scoped to what the Homarr connector does not already expose. Homarr has Docker start, stop and logs, DNS hole and system health. The gaps are array and parity, disk health, VMs, shares, notifications and mover. Anything that executes arbitrary commands is excluded.

### 10.3 `n8n` (proxy)

Upstream: n8n MCP server endpoint. Allow list only the workflow execution and data table tools by default.

### 10.4 `manifold` (native, always on)

Gateway self-management from inside Claude: `list_toolsets`, `toolset_health`, `recent_audit`. Read-only in v1.

## 11. Deployment

- Image: `ghcr.io/mannilie/manifold`, multi-stage Dockerfile (Node build stage for Astro, Python runtime), amd64 only. UID 99 and GID 100 are baked into the image. There is no root entrypoint and no privilege drop, so `PUID` and `PGID` are not supported.
- CI: GitHub Actions on push to `main` builds, runs tests, pushes `latest` and the git SHA tag.
- Unraid: Community Applications-style template XML in `deploy/unraid/manifold.xml`. Env vars: `MANIFOLD_MASTER_KEY`, `MANIFOLD_ADMIN_EMAILS`, `MANIFOLD_BASE_URL`, `MANIFOLD_LOG_LEVEL`, `MANIFOLD_DATA_DIR` (default `/data`). Volume: `/mnt/user/appdata/manifold:/data`. Port 8800 is published on the NAS loopback only, via `-p 127.0.0.1:8800:8800` in the template's extra parameters, never on a LAN interface. cloudflared runs with host networking and reaches it as `http://localhost:8800`.
- Watchtower: scoped label so only Manifold updates from this pipeline.
- Cloudflare Tunnel: public hostname `mcp.mannylab.cloud -> http://localhost:8800`.
- Cloudflare Access, Allow: one self-hosted app on `mcp.mannylab.cloud` with no path, policy allows Manny's email. It covers `/`, `/api` and `/oauth/authorize`, and anything not bypassed below.
- Cloudflare Access, Bypass: one self-hosted app with a Bypass policy for Everyone on each of `/healthz`, `/.well-known`, `/oauth/token`, `/oauth/register`, `/oauth/revoke`, and `/<toolset>` for every toolset (which also covers `/<toolset>/healthz`). Access matches the most specific path. Adding a toolset means adding one bypass app alongside the claude.ai connector.
- claude.ai: one custom connector per toolset with URL `https://mcp.mannylab.cloud/<toolset>`. No client ID or secret. The consent step opens a browser to `/oauth/authorize`, Access logs Manny in, Manifold issues the code.
- Local dev: `docker compose up` with a dev master key and SQLite in `./data`. Outside Docker, set `MANIFOLD_DATA_DIR` to a writable directory.

## 12. Observability

- Structured JSON logs to stdout.
- Every tool call written to `audit_log` with args hashed, never raw args, to avoid persisting spreadsheet contents or secrets.
- `/healthz` reports process health; `/<toolset>/healthz` reports upstream reachability and credential validity (cached, max once per 60 seconds).

## 13. Security

- No credential is ever logged, returned by the API in plaintext, or included in an export.
- Admin API and `/oauth/authorize` reject requests missing a valid Cloudflare Access email header even if Access is misconfigured. `/oauth/authorize` trusts that header only because the path is inside the Access Allow app; a request that reaches it any other way could spoof the header.
- Toolset endpoints require a bearer token issued by Manifold. Tokens are opaque random strings stored only as SHA-256 hashes. Access tokens live one hour, refresh tokens thirty days, both rotate on refresh, revoking one revokes its partner. A token issued with a resource indicator is refused by any other toolset.
- Dynamic client registration is open, as RFC 7591 intends, so redirect URIs are restricted to `https` on `claude.ai` and `claude.com`. Without that, a crafted authorize link would hand a stranger a code while Manny is logged in to Access.
- Port 8800 is bound to the NAS loopback only, so the header check is not the sole barrier against LAN access.
- OAuth `state` parameter is single-use and expires in 10 minutes.
- Proxy toolsets never forward Manifold's own headers to upstreams. Upstream auth is set explicitly from stored credentials.
- Rate limit per toolset, configurable, default 60 calls per minute.

## 14. Phases and completion criteria

**Phase 0: Spec agreed.** This document and CLAUDE.md reviewed by Manny. Done when Manny says go.

**Phase 1: Skeleton, deployed end to end.**
FastAPI app, `/healthz`, two toolsets (`manifold` with `ping`, and a second `ping-b` placeholder) mounted at their own paths, OAuth 2.1 server with in-memory state, Dockerfile, compose, GitHub Actions to GHCR, Unraid template, Cloudflare Tunnel and Access configured by Manny, both endpoints registered in claude.ai as separate connectors.
Done when: Manny calls `ping` on both connectors from claude.ai on his phone.

**Phase 2: Config store.**
SQLite schema, encrypted credentials, OAuth clients and tokens persisted so a restart does not force claude.ai to reconnect, toolset registry driven by the DB, reload on change, audit log writing, contract test harness, `manifold.example.yaml` export format.
Done when: enabling and disabling a toolset via a direct DB edit takes effect without a rebuild, and credentials round-trip through encryption.

**Phase 3: Admin UI.**
All five pages in section 9, credential forms for all auth kinds, OAuth flow with the Google preset working against a test app, defence-in-depth header check on `/api` and `/oauth/authorize`, CSRF header on mutations, per-tool disable, proxy tool discovery.
Done when: Manny adds a proxy toolset and completes a Google OAuth connect entirely from the browser.

**Phase 4: Sheets toolset.**
All tools in 10.1, both auth kinds, contract tests, integration test against a real throwaway spreadsheet.
Done when: Manny edits a real sheet from claude.ai using both a service account and an OAuth credential.

**Phase 5: Proxy toolsets.**
`unraid` and `n8n` configured via UI, tool discovery, allow/deny, prefixing, upstream health, graceful degradation.
Done when: Manny disconnects the standalone unraid-mcp connector in claude.ai and loses no capability.

**Phase 6: Hardening.**
Rate limits, audit retention, secret rotation runbook, backup and restore doc, `ping-b` placeholder removed.
Done when: runbooks written and a restore from backup tested.

## 15. Decision gates

Present options and wait at the start of every phase and before each of these:

- ASGI mounting strategy for multiple FastMCP servers (sub-app mount vs router)
- Hot reload of toolsets vs restart-on-change
- Encryption library and key derivation
- Astro output mode (static vs SSR)
- Settings form generation approach (JSON Schema renderer vs hand-built per toolset)
- Proxy implementation (MCP client library vs raw HTTP forwarding)
- Audit log retention and rollover
- Test strategy for OAuth flows

Record every decision in `DECISIONS.md` with date, options considered, choice, and reason.

## 16. Open questions

- Google OAuth app verification: a personal Google Cloud project in "testing" mode limits refresh tokens to 7 days. Either publish the app (internal is not available on a personal account) or accept periodic reconnects. Surface at the Phase 3 gate.
- Whether House Hunt should eventually migrate into Manifold as a native toolset. Out of scope for v1, note for later.
