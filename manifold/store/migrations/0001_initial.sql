-- Initial schema (DECISIONS.md, Phase 2 gate 2). Timestamps are ISO 8601 UTC text.

CREATE TABLE credentials (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    auth_kind   TEXT NOT NULL,
    scheme      TEXT NOT NULL,
    nonce       BLOB NOT NULL,
    ciphertext  BLOB NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE toolsets (
    key            TEXT PRIMARY KEY,
    display_name   TEXT NOT NULL,
    kind           TEXT NOT NULL CHECK (kind IN ('native', 'proxy')),
    enabled        INTEGER NOT NULL DEFAULT 0,
    credential_id  INTEGER REFERENCES credentials(id) ON DELETE RESTRICT,
    settings_json  TEXT NOT NULL DEFAULT '{}',
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);

CREATE TABLE proxy_upstreams (
    toolset_key   TEXT PRIMARY KEY REFERENCES toolsets(key) ON DELETE CASCADE,
    upstream_url  TEXT NOT NULL,
    prefix        TEXT,
    allow_json    TEXT NOT NULL DEFAULT '[]',
    deny_json     TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE gateway_settings (
    key         TEXT PRIMARY KEY,
    value_json  TEXT NOT NULL
);

-- One row, encrypted under the derived credentials key, verified at boot.
CREATE TABLE key_check (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    scheme      TEXT NOT NULL,
    nonce       BLOB NOT NULL,
    ciphertext  BLOB NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE oauth_clients (
    client_id      TEXT PRIMARY KEY,
    metadata_json  TEXT NOT NULL,
    created_at     TEXT NOT NULL
);

-- Authorization codes, access tokens and refresh tokens. token_hash is SHA-256 of the
-- raw token; the raw token is never stored. payload_json is the SDK model for the row.
CREATE TABLE oauth_tokens (
    token_hash    TEXT PRIMARY KEY,
    kind          TEXT NOT NULL CHECK (kind IN ('code', 'access', 'refresh')),
    client_id     TEXT NOT NULL REFERENCES oauth_clients(client_id) ON DELETE CASCADE,
    subject       TEXT,
    resource      TEXT,
    scopes_json   TEXT NOT NULL DEFAULT '[]',
    expires_at    REAL,
    partner_hash  TEXT,
    payload_json  TEXT NOT NULL,
    created_at    TEXT NOT NULL
);
CREATE INDEX oauth_tokens_expires_at ON oauth_tokens(expires_at);
CREATE INDEX oauth_tokens_client_id ON oauth_tokens(client_id);

CREATE TABLE oauth_state (
    state        TEXT PRIMARY KEY,
    toolset_key  TEXT NOT NULL,
    created_at   TEXT NOT NULL
);

CREATE TABLE audit_log (
    id           INTEGER PRIMARY KEY,
    ts           TEXT NOT NULL,
    toolset_key  TEXT NOT NULL,
    tool_name    TEXT NOT NULL,
    args_hash    TEXT NOT NULL,
    duration_ms  INTEGER NOT NULL,
    ok           INTEGER NOT NULL,
    error        TEXT
);
CREATE INDEX audit_log_ts ON audit_log(ts);
CREATE INDEX audit_log_toolset ON audit_log(toolset_key, ts);
