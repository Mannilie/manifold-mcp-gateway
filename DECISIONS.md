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

Correction, same day: the claude.ai auth row and the `portal_url` row were based on the belief that House Hunt used a Cloudflare MCP Server Portal. It does not; House Hunt implements OAuth itself. Both rows are superseded by the claude.ai auth gate below and `portal_url` is dropped.

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

## 2026-09-13: Phase 1 gate, how claude.ai authenticates to Manifold

Raised when the deploy reached Cloudflare and it turned out House Hunt's MCP server speaks OAuth itself. claude.ai custom connectors need either an OAuth handshake from the server or a URL with no auth at all; a plain Cloudflare Access application gives neither.

| Option | What it is | Trade-off | Cost to change later |
|---|---|---|---|
| A. Cloudflare MCP Server Portal | Cloudflare speaks OAuth to claude.ai, authenticates via Access, proxies to Manifold. No code. | Unverified in this account; unclear how the portal reaches an origin that is itself behind Access. Ties every connector to a beta feature. | Low if it works, dashboard-only debugging |
| B. Manifold speaks OAuth, Access is the login | Same shape as House Hunt. SDK supplies the routes and bearer middleware; Manifold supplies the provider. The authorize page sits behind Access. | About 400 lines with real security weight. Access bypass rules for the token, register, revoke, well-known and each toolset path. | High, every connector is bound to it |
| C. Secret in the URL | Per-toolset token in the connector URL, 401 without it. | Ten lines. Token in Cloudflare logs and claude.ai's stored URL. Revocation is rotate and reconnect. | Low, but every connector re-added on the way off |

**Choice:** B.

**Reason (Manny):** the pattern already running for House Hunt, keeps Access as the only human login, no dependency on a feature that cannot be verified.

Details settled while building it, cheap to change:

- Issuer is the bare origin and the endpoints live under `/oauth/`. RFC 8414 allows it and a path-less issuer is what every client tries first.
- Streams are stateless already, so a stray GET on a toolset gets 401 without a token and 405 with one.
- Tokens are opaque, stored as SHA-256 only. Access one hour, refresh thirty days, rotated on refresh, revoke either to revoke both.
- Every token is bound to exactly one toolset. The authorize request must carry an RFC 8707 `resource` naming a mounted toolset URL, or it fails with `invalid_target`. A token for `/sheets` is refused at `/unraid`. Manny's condition: one connector, one token, one toolset. Risk: if claude.ai ever omits `resource`, no connector can authorise; the fallback would be accepting unbound tokens, which is a one-line change in the provider.
- Dynamic registration is open but redirect URIs must be `https` on `claude.ai` or `claude.com`. This is the only thing standing between a crafted authorize link and a stranger holding a code.
- `MANIFOLD_ADMIN_EMAILS` is now required at boot. Without it nobody can authorise a connector.
- The provider talks to a `TokenStore` protocol. Phase 1 ships `InMemoryTokenStore`; Phase 2 adds a SQLite store behind the same protocol and the handlers do not change. `tests/contract/test_token_store.py` is parametrised over implementations for that reason.
- Phase 1 keeps clients and tokens in memory. Every restart logs out every connector until Phase 2 persists them, which is Phase 2's first deliverable because Watchtower restarts on every push to main.
- Dynamic registration is rate limited globally to 30 per 10 minutes and capped at 100 stored clients, both answered with 429. Single user, one origin, so per-address limits would add nothing behind Cloudflare. "Disconnect all" in Phase 3 clears the store.
- PKCE S256 is mandatory and `plain` is rejected. That is the SDK's default and a test now pins it.

## 2026-09-13: Phase 1 complete, Phase 2 gate order

Phase 1 done: Manny called `ping` on both connectors from the phone. Manny set the Phase 2 gate order as encryption, then schema, then hot reload, because the schema depends on how ciphertext is stored and hot reload depends on the schema. After the gates, the SQLite `TokenStore` ships and deploys on its own before the rest of Phase 2, so restarts stop logging out the connectors early.

## 2026-09-13: Phase 2 gate 1, encryption library and key derivation

| Option | What it is | Trade-off | Cost to change later |
|---|---|---|---|
| A. `cryptography` Fernet | AES-128-CBC plus HMAC in a versioned token, `MultiFernet` rotation | Simplest API, but no associated data, 128-bit key, CBC, unwanted timestamp, spec `nonce` column unused | Medium, re-encrypt every row |
| B. `cryptography` AES-256-GCM with HKDF-SHA256 | AEAD, random 96-bit nonce per write, associated data binds the row, `scheme` column for rotation | Same dependency as A, about 40 lines of our own code with tests, matches the spec table | Medium, but the `scheme` column makes it a planned path |
| C. `pynacl` SecretBox | libsodium XSalsa20-Poly1305 | Second native dependency for no gain, no associated data in the high-level API | Medium |

Key derivation: raw master key used directly, or HKDF-SHA256 per purpose. HKDF chosen.

**Choice:** B with HKDF-SHA256 per purpose.

**Conditions (Manny):**

- Associated data is `scheme || credential_id || auth_kind`, so editing the `scheme` column cannot downgrade a ciphertext to an older scheme, and a ciphertext moved to another credential row fails. (Amended at gate 2, same day: credentials became a first-class table keyed by their own id and shared between toolsets, so the row identity in the associated data is the credential id, not a toolset key.)
- HKDF `info` strings are constants in one module and listed here. Initial list: `manifold/credentials/v1` (credential ciphertext), `manifold/key-check/v1` (the boot-time key check). Any new purpose is appended to this list in the same commit that adds it.
- The `key_check` row is encrypted under the derived credentials key, not the raw master key, so it proves derivation is stable across versions as well as proving the master key is right.
- Tests: ciphertext moved to another credential row fails to decrypt; ciphertext with the `scheme` column edited fails; wrong master key fails at boot with the clear message.

## 2026-09-13: Phase 2 gate 2, schema and migrations

| Option | What it is | Trade-off | Cost to change later |
|---|---|---|---|
| A. Numbered SQL files, `PRAGMA user_version` | `store/migrations/NNNN_name.sql` applied in order at boot, each in its own transaction, database file copied aside first | No dependency, reviewable SQL diffs, destructive changes by hand, the copy is the Phase 6 backup primitive | Low |
| B. Alembic with SQLAlchemy Core | Migration tool with autogenerate plus a query builder | Two large dependencies and a second table description for a schema this size | Medium |
| C. Single idempotent `schema.sql` | `CREATE TABLE IF NOT EXISTS` and guarded `ALTER`s | Smallest, but no version record and no clean path for a non-additive change | Low until the first non-additive change |

**Choice:** A.

**Schema changes (Manny):**

- `credentials` is a first-class table keyed by its own id: id, name, auth_kind, scheme, nonce, ciphertext, created_at, updated_at. `toolsets` gains a nullable `credential_id` foreign key. Reason: drive, docs and sheets will share one Google credential, and the UI shows "Google (Manny)" as a pickable credential rather than three copies. Deleting a credential that is in use is refused with the list of toolsets using it.
- Authorization codes live in `oauth_tokens` with kind `code`.
- `oauth_clients` has `created_at`. `oauth_tokens` has an index on `expires_at` so expiry sweeps are cheap.
- Pre-migration copies are kept to the last 5 (`manifold.db.pre-<version>-<timestamp>`), older ones deleted after a successful migration, so a year of deploys cannot fill appdata.
