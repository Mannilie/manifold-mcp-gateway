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
- HKDF `info` strings are constants in one module and listed here. Current list: `manifold/credentials/v1` (credential ciphertext; the key check row uses this same key with its own associated data, so no second purpose is needed yet). Any new purpose is appended to this list in the same commit that adds it.
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

## 2026-09-13: Phase 2 gate 3, hot reload versus restart on change

| Option | What it is | Trade-off | Cost to change later |
|---|---|---|---|
| A. Restart on change | Config writes mark "restart required", Settings restart button exits the process | 20 lines, seconds of downtime, direct DB edits need `docker restart` | Low |
| B. Hot reload on API trigger | Registry diffs desired against mounted after each API write and on `POST /api/reload` | 120 lines plus tests, direct DB edits wait for a trigger | Low |
| C. Hot reload plus change detection | B plus a task polling `PRAGMA data_version` every few seconds | B plus 30 lines, direct DB edits take effect within seconds | Low |

**Choice:** C.

**Conditions (Manny):**

- Diff by content hash of (settings_json, credential_id, enabled, upstream row). A reload rebuilds only toolsets that changed. Editing sheets must not restart unraid.
- The mapping swap is atomic: build every changed runtime first, then swap the dict once. A build failure leaves the old runtime mounted if there was one, reports health down with the error, and never leaves a previously mounted key unmounted.
- Reloads are serialised. A trigger arriving during a run is coalesced into one follow-up run, never a queue.
- Old runtimes get a short drain window for in-flight calls before their exit stack closes.
- The restart button stays on the Settings page as the escape hatch.
- Tests: change one of two toolsets and the other's runtime object is the same instance; `build()` raising leaves the prior runtime mounted; two concurrent triggers produce exactly one extra run.

Delivery order after the gates: SQLite `TokenStore` first, deployed alone, then the rest of Phase 2.

## 2026-09-13: Phase 2 build notes

Settled while building, none expensive to reverse.

- The content hash also includes the credential's `updated_at`, so rotating a shared credential rebuilds every toolset using it. Display name is not in the hash; renaming a card never restarts anything.
- A toolset whose last build failed is retried on every reload even if its hash is unchanged, so a transient failure (upstream down at boot) heals on the next change or trigger without a restart.
- Each runtime's session manager runs in a task of its own. anyio cancel scopes must exit in the task that entered them, and reloads start and stop runtimes from whichever task triggered them.
- Audit log error text: for an MCP error the message is stored, truncated to 200 characters; for any other exception only the type name is stored, because arbitrary exception messages can echo argument values.
- Native toolsets are seeded with their manifest's `example_settings` when first registered, so enabling one without visiting the UI produces a buildable config.
- Log level from the database is applied after the store opens; the env var covers boot.

## 2026-09-13: Phase 2 complete

Manny enabled `ping-b` with a direct database edit on the NAS; the gateway mounted it within seconds without a restart and the claude.ai connector answered on its existing token. Credential encryption round-trips are covered by the unit and contract tests.

## 2026-09-13: Phase 3 gate 1, Astro output mode

| Option | What it is | Trade-off | Cost to change later |
|---|---|---|---|
| A. Static build | Page shells built at image build time, FastAPI serves them, pages fetch `/api` from the browser | No Node at runtime, one process; empty shell until the API answers | Medium |
| B. SSR with Node adapter | Astro runs as a server in the container and renders per request | Filled-in first paint; second runtime, second process, header forwarding to FastAPI | Medium |

**Choice:** A, static.

**Conditions (Manny):**

- Mutating `/api` requests must carry `X-Manifold-Request: 1` and the API rejects any without it. The browser is authenticated by the Access cookie, so without this a page on another origin could POST to `/api` with Manny's session.
- `index.html` and API responses are served `Cache-Control: no-store`; hashed assets get a long cache lifetime.
- Unknown paths that are not a toolset key, a reserved path or an asset fall through to `index.html` so client-side routing survives a refresh.
- Every page has loading and error states. An empty shell that never fills in because `/api` failed must say so.

## 2026-09-13: Phase 3 gate 2, settings form generation

| Option | What it is | Trade-off | Cost to change later |
|---|---|---|---|
| A. Own renderer, constrained subset, no framework | About 300 lines of TypeScript for string, number, integer, boolean, enum and array-of-string; the contract test rejects schemas outside the subset | No dependency; nested objects wait for the subset to grow | Low to medium, schemas stay standard |
| B. Library renderer | JSON Forms or react-jsonschema-form | Full coverage, but brings React or Vue and a styling job | Medium |
| C. Hand-built form per toolset | A TypeScript component per toolset | Breaks the "one Python package" goal | Medium |

**Choice:** A. The UI stays framework-free vanilla TypeScript islands.

**Conditions (Manny):**

- The server validates settings against the same schema on write. The renderer is a convenience, not the guard.
- Credentials never appear in `settings_schema`. A secret-looking property name (password, token, secret, key) fails the contract test with a pointer to the credentials mechanism.
- A small `x-manifold` extension carries UI hints: placeholder, help link, multiline. Any other unknown keyword fails the contract test.
- Array-of-string gets a real add, remove and reorder editor, one value per row, because spreadsheet IDs are pasted one at a time.
- Navigating away from a dirty form asks first.

## 2026-09-13: Phase 3 gate 3, test strategy for upstream OAuth flows

| Option | What it is | Trade-off | Cost to change later |
|---|---|---|---|
| A. Fake provider in-process | Test ASGI app implementing authorize, token, refresh and failure modes; tests drive the whole Manifold flow | Fast, deterministic, covers our code; proves nothing about Google's quirks | Low |
| B. Recorded cassettes against Google | Replay captured token exchanges | Authorize step cannot be recorded; real tokens to scrub; stale silently | Low, low value |
| C. Live test against Manny's Google client | Runs only with env vars pointing at a real client and refresh token | The only proof the Google preset works; cannot cover first connect | Low |

**Choice:** A in CI, C as the manual end-of-phase check. B skipped.

**Conditions (Manny):**

- The fake provider enforces the Google-specific parameters: the connect fails if `access_type=offline` and `prompt=consent` are missing from the authorize request, and a refresh token is returned only when they are present.
- Refresh is coalesced: two tool calls hitting an expired token at once produce one refresh, and the second waits for it.
- Refresh failure with `invalid_grant` flips the toolset to degraded with a "reconnect" action in the UI; a transient 5xx does not.
- Callback state is single-use and bound to the credential id. A callback for a state already consumed is rejected without touching the stored token.

Google client publishing: to be published to Production once the scope gate below is decided. Testing status expires refresh tokens after seven days.

## 2026-09-13: Phase 3 gate 4, Google scopes and client publishing

| Option | Scopes on the Google credential | Trade-off |
|---|---|---|
| A. Sheets only | `spreadsheets` | Least privilege for Phase 4 |
| B. Sheets plus `drive.file` | `spreadsheets`, `drive.file` | `drive.file` cannot reach a pasted spreadsheet ID |
| C. Sheets plus full Drive read | `spreadsheets`, `drive.readonly` | Read on everything before anything uses it |

**Choice:** A, `https://www.googleapis.com/auth/spreadsheets` only for Phase 4. The Google OAuth client is published to Production so refresh tokens do not expire after seven days; the unverified-app warning is clicked through once per credential.

**Growth path:** add `documents` (sensitive) when the docs toolset lands, `drive.readonly` (restricted) when the drive toolset lands. Each widening is a scope edit on the credential and one reconnect. Google's restricted-scope verification page lists "you are the only user of your app" among the cases where verification is not required, and its audience page describes unverified Production apps as warned and capped at 100 users, not blocked: https://developers.google.com/identity/protocols/oauth2/production-readiness/restricted-scope-verification and https://support.google.com/cloud/answer/15549945

**Conditions (Manny):**

- Scopes are editable on the credential in the UI. Any change marks the credential "reconnect required" and disables dependent toolsets until reconnected. Widening scopes never silently reuses the old refresh token.
- The granted scope string from the token response is stored, not the requested one, and both are shown on the credential page. Google can return fewer than asked.

## 2026-09-13: Phase 3 build notes

Settled while building, none expensive to reverse.

- The UI is one `index.html` shell plus hashed assets. Astro is the build tool and layout; routing, data loading and forms are a small vanilla TypeScript app. `/_astro`, `/assets` and `/static` reach the UI; `/api`, `/healthz` and `/oauth` stay 404 when nothing else served them.
- Per-tool disable is a server middleware on the toolset's MCP server: hidden from `tools/list`, refused on `tools/call`. It is part of the content hash, so toggling a tool rebuilds only that toolset.
- Editing an oauth2 credential's client secret is treated like a scope change: stored tokens are discarded and the credential needs a reconnect.
- Proxy tool discovery uses the SDK client against the upstream with headers from the stored credential. This does not pre-empt the Phase 5 gate on how proxying itself is implemented.
- A toolset that is not mounted lists its tools by building a throwaway server with example settings, so the detail page is useful before enabling.
- The restart button sends SIGTERM to the process after the response is written; Docker's restart policy brings it back.
- The Google client is published to Production so refresh tokens do not expire after seven days. Recorded at gate 4; done by Manny in Google Cloud Console.
