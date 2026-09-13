-- Phase 6: admin actions in the audit log. actor is the Cloudflare Access email; detail a
-- short human note such as "credential 'n8n MCP' (bearer)". Tool calls leave both null.

ALTER TABLE audit_log ADD COLUMN actor TEXT;
ALTER TABLE audit_log ADD COLUMN detail TEXT;
