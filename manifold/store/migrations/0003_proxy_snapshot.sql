-- Phase 5: upstream tool snapshot per proxy, and the upstream tool name in the audit log.

ALTER TABLE proxy_upstreams ADD COLUMN tools_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE audit_log ADD COLUMN upstream_tool TEXT;
