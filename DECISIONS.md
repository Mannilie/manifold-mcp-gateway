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

## 2026-09-13: Phase 1 gate, ASGI mounting strategy

Measured on `mcp` 2.2.0 and Starlette 1.6 with two servers, POST `initialize`, redirects disabled.

| Option | What it is | POST `/<key>` | Trade-off | Cost to change later |
|---|---|---|---|---|
| A. Starlette `Mount` of the SDK sub-app | `streamable_http_app(streamable_http_path="/")` mounted at `/<key>` | 307 to `/<key>/`, or 404 with redirects off | Least code but cannot serve `/<key>` without a redirect. Sub-app lifespans do not run under `Mount`. | Low |
| B. Exact `Route` per toolset to the SDK transport app | `Route("/<key>")` and `Route("/<key>/healthz")` in the Starlette route table | 200 | Works today. Phase 2 hot reload must mutate Starlette's route list at runtime. | Low to medium |
| C. Custom dispatcher on first path segment | One ASGI callable mounted last, registry dict lookup, exact `/<key>` to the transport app, `/<key>/healthz` to health, unknown keys fall through to the UI | 200 | About 40 lines Manifold owns and tests. Hot reload is a dict swap. | Low |

**Choice:** C, custom dispatcher.

**Reason (Manny):** meets the no-redirect requirement and does not fight Starlette's route table when Phase 2 adds hot reload.

**Conditions (Manny):**

- The contract test covers: exact `/<key>` POST 200, `/<key>/` 200, `/<key>/healthz` 200, `/<key>/anything-else` 404, reserved keys 404, unknown key falls through to the UI, and no 3xx response anywhere.
- The dispatcher is one module with no Starlette imports beyond the ASGI types, so the Phase 2 hot reload test can exercise it in isolation.
- The SDK's DNS-rebinding protection is off for every toolset transport, because Cloudflare terminates the public hostname and the Host header is not `localhost`. This is safe only because port 8800 is never published on the Unraid host (Phase 0 amendment above). If 8800 is ever published, rebinding protection must be turned on with `mcp.mannylab.cloud` as the allowed host. The two decisions are linked.

## 2026-09-13: Phase 1 non-gated transport picks

Cheap to flip, recorded so they are not re-derived.

- Streamable HTTP in stateless mode. A gateway that will hot-reload toolsets in Phase 2 must not hold per-session server state that a reload would invalidate. Cost: no server-initiated notifications over a GET stream. Not needed in v1.
- JSON responses rather than SSE for POST replies. Plain JSON is simpler through Cloudflare and on a phone. Flip `json_response` if a toolset ever needs streaming progress.
- Native toolset package directories use underscores where the key has a hyphen (`toolsets/ping_b` serves key `ping-b`). The key comes from `MANIFEST.key`, never from the directory name.
- `httpx2` is the only HTTP client library. `mcp` 2.x depends on `httpx2` (the httpx 2 line, published under that name) and its client takes an `httpx2.AsyncClient`. Adding `httpx` as well would ship two copies of the same library. CLAUDE.md updated to match.
- Toolset endpoints answer GET with 405. In stateless mode there is no server-initiated stream to offer, but the SDK would still hold a GET open as an idle SSE stream. The MCP spec allows 405, and it stops a stray GET tying up a connection.

## 2026-09-13: Phase 1 deploy, how cloudflared reaches Manifold

Found during the Unraid deploy: cloudflared runs with host networking, so it cannot resolve Docker network names. This supersedes the "never published on the host" wording in the Phase 0 amendments.

| Option | What it is | Trade-off | Cost to change later |
|---|---|---|---|
| A. Publish 8800 on loopback only | `-p 127.0.0.1:8800:8800`, tunnel route to `http://localhost:8800` | Reachable from the NAS itself, not the LAN. Same pattern as the other tunnel routes. | Low |
| B. Move cloudflared to a custom Docker network | `mcpnet` shared by cloudflared and Manifold, route to `manifold:8800` | Keeps the original wording but breaks every existing `localhost:<port>` tunnel route until rewritten. | Medium, outside this project |

**Choice:** A.

**Reason (Manny):** matches the working pattern, keeps the LAN out, one parameter.

The DNS-rebinding link from the mounting gate still holds: a LAN browser cannot reach the NAS loopback, so rebinding protection stays off. If 8800 is ever bound to a LAN interface, turn it on with `mcp.mannylab.cloud` as the allowed host.
