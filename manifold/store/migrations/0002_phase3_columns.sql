-- Phase 3: credential status and non-secret metadata, per-tool disable, token refresh
-- timestamps that do not count as a config change.

ALTER TABLE credentials ADD COLUMN status TEXT NOT NULL DEFAULT 'ok'
    CHECK (status IN ('ok', 'unconnected', 'reconnect_required'));
-- Non-secret facts shown in the UI without decrypting: client_email, client_id, provider,
-- requested scopes, granted scope, access token expiry.
ALTER TABLE credentials ADD COLUMN meta_json TEXT NOT NULL DEFAULT '{}';
-- Bumped by automatic token refresh instead of updated_at, so a refresh never looks like
-- a config change to the registry's content hash.
ALTER TABLE credentials ADD COLUMN token_updated_at TEXT;

ALTER TABLE toolsets ADD COLUMN disabled_tools_json TEXT NOT NULL DEFAULT '[]';
