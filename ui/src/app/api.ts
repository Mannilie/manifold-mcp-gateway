// Thin client for /api. Mutations carry X-Manifold-Request, the CSRF guard the server
// requires (DECISIONS.md, Phase 3 gate 1).

export class ApiError extends Error {
  constructor(public status: number, public detail: unknown) {
    super(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
}

async function call<T>(method: string, path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json" };
  if (method !== "GET") headers["X-Manifold-Request"] = "1";
  if (body !== undefined) headers["Content-Type"] = "application/json";
  let res: Response;
  try {
    res = await fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body), credentials: "same-origin" });
  } catch (e) {
    throw new ApiError(0, `network error: ${(e as Error).message}`);
  }
  if (res.status === 204) return undefined as T;
  const text = await res.text();
  let data: unknown = text;
  try { data = text ? JSON.parse(text) : null; } catch { /* keep text */ }
  if (!res.ok) {
    const detail = (data && typeof data === "object" && "detail" in (data as object)) ? (data as { detail: unknown }).detail : data;
    throw new ApiError(res.status, detail);
  }
  return data as T;
}

export const api = {
  get: <T>(path: string) => call<T>("GET", path),
  post: <T>(path: string, body?: unknown) => call<T>("POST", path, body),
  patch: <T>(path: string, body: unknown) => call<T>("PATCH", path, body),
  del: <T>(path: string) => call<T>("DELETE", path),
};

export function describe(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.status === 401) return "Not signed in through Cloudflare Access.";
    if (err.status === 403) return "Not allowed. " + err.message;
    if (err.status === 0) return err.message;
    if (typeof err.detail === "object" && err.detail !== null) {
      const d = err.detail as Record<string, unknown>;
      if (Array.isArray(d.settings)) return "Settings rejected: " + d.settings.join("; ");
      if (typeof d.message === "string") return d.message;
      return JSON.stringify(err.detail);
    }
    return String(err.detail);
  }
  return err instanceof Error ? err.message : String(err);
}

// -- types mirroring manifold/api/models.py --------------------------------------------

export interface Health { status: string; detail: string }
export interface Checklist { connector_url: string; bypass_path: string; covers: string[] }
export interface ToolsetSummary {
  key: string; display_name: string; kind: string; enabled: boolean; mounted: boolean; health: Health;
  endpoint_url: string; tool_count: number; last_call_at: string | null; credential_id: number | null;
  credential_name: string | null; error: string | null; cloudflare: Checklist;
}
export interface Tool { name: string; description: string; enabled: boolean }
export interface ToolsetDetail extends ToolsetSummary {
  version: string | null; supported_auth: string[]; settings_schema: Schema; settings: Record<string, unknown>;
  tools: Tool[]; disabled_tools: string[]; upstream: { upstream_url: string; prefix: string | null; allow: string[]; deny: string[] } | null;
}
export interface Credential {
  id: number; name: string; auth_kind: string; status: string; meta: Record<string, unknown>; used_by: string[];
  created_at: string; updated_at: string; token_updated_at: string | null;
}
export interface AuditEntry {
  id: number; ts: string; toolset_key: string; tool_name: string; args_hash: string; duration_ms: number; ok: boolean; error: string | null;
  upstream_tool: string | null; actor: string | null; detail: string | null;
}
export interface Settings {
  log_level: string; log_level_source: string; audit_retention_days: number; master_key: { verified: boolean; first_run_at: string | null };
  base_url: string; admin_emails: string[]; version: string; schema_version: number; oauth_clients: number;
}
export interface SchemaProperty {
  type: "string" | "number" | "integer" | "boolean" | "array"; title?: string; description?: string; default?: unknown;
  enum?: string[]; minimum?: number; maximum?: number; minLength?: number; maxLength?: number; pattern?: string;
  items?: { type: "string" }; "x-manifold"?: { placeholder?: string; help_url?: string; multiline?: boolean };
}
export interface Schema { type: "object"; properties?: Record<string, SchemaProperty>; required?: string[]; title?: string; description?: string }
