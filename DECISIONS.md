# Decisions

Append-only log of decisions taken at gates. Newest at the bottom. Each entry records the date, the options considered, the choice and the reason. Do not re-open a decision here without a new entry that supersedes it.

## 2026-09-13: Phase 0 spec amendments

Raised by Claude Code in Phase 0 review, accepted by Manny. All applied to SPEC.md in commit `730a4f6`.

| Topic | Options | Choice | Reason |
|---|---|---|---|
| claude.ai auth through Cloudflare | Access for SaaS OIDC with client ID and secret; MCP Server Portal with Access Managed OAuth; Manifold serves OAuth metadata itself | MCP Server Portal, portal URL only pasted into claude.ai | House Hunt already works this way. Manifold implements no client OAuth. |
| Container user | Bake UID 99 GID 100; root entrypoint with PUID and PGID drop | Bake 99:100, drop PUID and PGID | No root in the container, no entrypoint to maintain, template stops claiming something it does not do. |
| Log level source | Env only; DB only; env boot default with DB override | Env boot default, DB override once loaded | Logs are needed before the DB is open. Runtime changes belong in the UI. |
| Data directory | Hard-code `/data`; add `MANIFOLD_DATA_DIR` | Add `MANIFOLD_DATA_DIR`, default `/data` | Tests and local runs outside Docker need a writable path. |
| Defence in depth on toolset paths | Admin only; admin and toolsets; full Access JWT verification | Email header check on `/<toolset>` too, from Phase 3. Port 8800 never published on the host. | JWT verification needs two more env vars and is not worth it for a single user when the port is not reachable from the LAN. |
| Reserved toolset keys | None; explicit list | Reserve `api`, `healthz`, `oauth`, `assets`, `_astro`, `static` | They collide with paths the gateway already serves. |
| Manifest example settings | Contract test invents settings; manifest carries `example_settings` | Add `example_settings` to the manifest | The contract test needs known-valid settings per toolset. |
| Default state of newly discovered native toolsets | Enabled; disabled | Disabled, except `manifold` which is always on | A code deploy should not expose a new endpoint until it is deliberately enabled. |
| Portal URL on the dashboard | Show origin URL only; add optional `portal_url` per toolset | Add `portal_url`, copy button prefers it | The origin path is not what gets pasted into claude.ai. |

## 2026-09-13: Phase 1 gate, MCP server library

| Option | What it is | Trade-off | Cost to change later |
|---|---|---|---|
| Official `mcp` SDK, 2.x | `MCPServer` from `mcp.server.mcpserver` (renamed from `FastMCP` in 2.0), client library included | Smallest surface, tracks the protocol, no built-in proxy helper so Phase 5 writes its own forwarding on the SDK client | High. Every toolset's `build()` returns one of these. |
| `fastmcp` v2 | Third-party superset of the SDK with `as_proxy()`, mounting helpers, middleware | Large surface that moves fast, version drift against the SDK it wraps | High, same reason. |

**Choice:** official `mcp` SDK 2.x. Pinned `mcp>=2.2,<3`.

**Reason (Manny):** smaller surface, tracks the spec, toolset contract stays portable. Claude Code to push back only if `fastmcp` v2 offers something essential for multi-mount or proxying. Verified on 2.2.0: multi-mount needs nothing beyond the SDK (see the mounting gate below) and proxying can be built on `mcp.client` with an allow and deny filter, so no push-back.

Note: SPEC.md and CLAUDE.md say "FastMCP". In `mcp` 2.x that class is `MCPServer`. The spec's meaning is unchanged; the code uses `MCPServer`.

## 2026-09-13: Phase 1 gate, container user

Settled as part of the Phase 0 amendments above. Bake UID 99 GID 100 into the image. No PUID or PGID.

## 2026-09-13: Phase 1 non-gated picks

Recorded for the log, accepted by Manny without a gate because each is cheap to reverse.

- Base image `python:3.12-slim` with the `uv` binary copied in from the official `uv` image.
- amd64 only. Unraid is the only target.
- GitHub Actions on push to `main`: run tests, then build and push `latest` and the git SHA tag to GHCR. Tests gate the push.
- Python pinned to 3.12 via `uv` and `.python-version`. The system interpreter is never used.
