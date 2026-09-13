import { api, describe, type Credential, type ToolsetSummary } from "../api";
import { clear, confirmDialog, fmtTime, h } from "../dom";
import { navigate, setDirty, type Page } from "../router";
import { listEditor } from "../schemaForm";

const KINDS = ["api_key", "basic", "bearer", "service_account", "oauth2"] as const;
const DIRTY = "You have unsaved changes. Leave anyway?";

export const credentialsPage: Page = async (root) => {
  const list = await api.get<Credential[]>("/api/credentials");
  clear(root);
  root.append(h("h1", {}, "Credentials"));
  if (list.length === 0) root.append(h("p", { class: "muted" }, "No credentials yet."));
  else {
    const table = h("table", {}, h("thead", {}, h("tr", {}, h("th", {}, "Name"), h("th", {}, "Kind"), h("th", {}, "Status"), h("th", {}, "Used by"), h("th", {}, "Updated"))));
    for (const c of list) {
      table.append(h("tr", {},
        h("td", {}, h("a", { href: `/credentials/${c.id}`, "data-link": true }, c.name)),
        h("td", {}, c.auth_kind),
        h("td", {}, statusTag(c.status)),
        h("td", {}, c.used_by.length ? c.used_by.join(", ") : h("span", { class: "muted" }, "nothing")),
        h("td", { class: "muted" }, fmtTime(c.updated_at)),
      ));
    }
    root.append(table);
  }
  root.append(h("h2", {}, "Add credential"), createForm());
};

function statusTag(status: string): HTMLElement {
  const text: Record<string, string> = { ok: "Ready", unconnected: "Not connected", reconnect_required: "Reconnect required", scopes_changed: "Reconnect required (scopes changed)" };
  return h("span", { class: status === "ok" ? "" : "error" }, text[status] ?? status);
}

function field(labelText: string, control: HTMLElement, help?: string): HTMLElement {
  return h("label", { class: "field" }, h("span", {}, labelText), control, help ? h("small", {}, help) : null);
}

function createForm(): HTMLElement {
  const name = h("input", { type: "text", placeholder: "Google (Manny)" });
  const kind = h("select", {}, ...KINDS.map((k) => h("option", { value: k }, k)));
  const body = h("div", {});
  const notice = h("p", {});
  const submit = h("button", { type: "button", class: "primary" }, "Add credential");
  let collect: () => Record<string, unknown> = () => ({});
  const scopes = listEditor([], "https://www.googleapis.com/auth/spreadsheets", () => setDirty(DIRTY));

  const draw = () => {
    body.replaceChildren();
    const k = kind.value;
    const inputs: Record<string, HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement> = {};
    const add = (key: string, label: string, el: HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement, help?: string) => { inputs[key] = el; body.append(field(label, el, help)); };
    if (k === "api_key") { add("key", "API key", h("input", { type: "password", autocomplete: "off" })); add("header", "Header name", h("input", { type: "text", value: "X-API-Key" })); }
    if (k === "basic") { add("username", "Username", h("input", { type: "text" })); add("password", "Password", h("input", { type: "password", autocomplete: "off" })); }
    if (k === "bearer") add("token", "Token", h("input", { type: "password", autocomplete: "off" }));
    if (k === "service_account") {
      const ta = h("textarea", { placeholder: "Paste the service account JSON" });
      const file = h("input", { type: "file", accept: "application/json" });
      file.addEventListener("change", async () => { const f = file.files?.[0]; if (f) ta.value = await f.text(); });
      add("json", "Service account JSON", ta, "The client_email is shown after saving. The key is encrypted at rest.");
      body.append(field("Or upload the file", file));
    }
    if (k === "oauth2") {
      const provider = h("select", {}, h("option", { value: "google" }, "Google"), h("option", { value: "microsoft" }, "Microsoft"), h("option", { value: "generic" }, "Generic"));
      add("provider", "Provider", provider);
      add("client_id", "Client ID", h("input", { type: "text" }));
      add("client_secret", "Client secret", h("input", { type: "password", autocomplete: "off" }));
      const urls = h("div", {});
      const authUrl = h("input", { type: "url" }); const tokenUrl = h("input", { type: "url" });
      inputs.auth_url = authUrl; inputs.token_url = tokenUrl;
      urls.append(field("Authorization URL", authUrl), field("Token URL", tokenUrl));
      urls.hidden = true;
      provider.addEventListener("change", () => { urls.hidden = provider.value !== "generic"; });
      body.append(urls);
      body.append(field("Scopes", scopes.element, "One scope per row. Google Sheets needs https://www.googleapis.com/auth/spreadsheets"));
    }
    collect = () => {
      const values: Record<string, unknown> = {};
      for (const [key, el] of Object.entries(inputs)) if (!["provider", "auth_url", "token_url"].includes(key)) values[key] = el.value;
      const out: Record<string, unknown> = { name: name.value.trim(), auth_kind: k, values };
      if (k === "oauth2") { out.provider = inputs.provider?.value; out.scopes = scopes.get(); out.auth_url = inputs.auth_url?.value || null; out.token_url = inputs.token_url?.value || null; }
      return out;
    };
  };
  kind.addEventListener("change", draw);
  draw();
  submit.addEventListener("click", async () => {
    submit.disabled = true; notice.textContent = "Saving"; notice.className = "muted";
    try {
      const created = await api.post<Credential>("/api/credentials", collect());
      setDirty(null);
      navigate(`/credentials/${created.id}`);
    } catch (err) { notice.textContent = describe(err); notice.className = "error"; submit.disabled = false; }
  });
  return h("div", { class: "card" }, field("Name", name), field("Kind", kind), body, h("div", { class: "row" }, submit), notice);
}

export const credentialPage: Page = async (root, params, query) => {
  const id = params.id ?? "";
  const [c, toolsets] = await Promise.all([api.get<Credential>(`/api/credentials/${id}`), api.get<ToolsetSummary[]>("/api/toolsets")]);
  clear(root);
  const notice = h("p", {});
  const say = (text: string, cls = "muted") => { notice.textContent = text; notice.className = cls; };
  root.append(h("p", {}, h("a", { href: "/credentials", "data-link": true }, "← Credentials")), h("h1", {}, c.name), h("p", {}, h("span", { class: "tag" }, c.auth_kind), " ", statusTag(c.status)));
  if (query.get("connected")) root.append(h("div", { class: "banner ok" }, "Connected. Toolsets using this credential are being reloaded."));

  const meta = c.meta;
  const dl = h("dl", { class: "kv" });
  const row = (k: string, v: unknown) => { if (v !== undefined && v !== null && v !== "") dl.append(h("dt", {}, k), h("dd", {}, Array.isArray(v) ? v.join(" ") : String(v))); };
  row("Client email", meta.client_email); row("Project", meta.project_id); row("Username", meta.username); row("Header", meta.header);
  row("Provider", meta.provider); row("Client ID", meta.client_id);
  row("Requested scopes", meta.scopes); row("Granted scope", meta.granted_scope ?? (c.auth_kind === "oauth2" ? "not connected yet" : undefined));
  row("Connected at", meta.connected_at ? fmtTime(String(meta.connected_at)) : undefined);
  row("Token refreshed", c.token_updated_at ? fmtTime(c.token_updated_at) : undefined);
  row("Used by", c.used_by.length ? c.used_by.join(", ") : "nothing");
  root.append(dl, notice);

  if (c.auth_kind === "oauth2") {
    const connectBtn = h("button", { type: "button", class: "primary", onclick: async () => {
      connectBtn.disabled = true;
      try { const r = await api.post<{ authorize_url: string }>(`/api/credentials/${id}/connect`); location.href = r.authorize_url; }
      catch (err) { say(describe(err), "error"); connectBtn.disabled = false; }
    } }, c.status === "ok" ? "Reconnect" : "Connect");
    root.append(h("h2", {}, "Connection"), h("p", { class: "muted" }, c.status === "ok" ? "Connected. Reconnect if the provider revoked access." : "Complete the provider consent to start using this credential."), h("div", { class: "row" }, connectBtn));
    const scopes = listEditor((meta.scopes as string[] | undefined) ?? [], "scope", () => setDirty(DIRTY));
    root.append(h("h2", {}, "Scopes"), h("p", { class: "muted" }, "Changing scopes requires a reconnect. Toolsets using this credential stop until then."), scopes.element,
      h("div", { class: "row" }, h("button", { type: "button", onclick: async () => {
        try { await api.patch(`/api/credentials/${id}`, { scopes: scopes.get() }); setDirty(null); navigate(`/credentials/${id}`, { replace: true }); } catch (err) { say(describe(err), "error"); }
      } }, "Save scopes")));
  }

  // Rename and replace values
  const nameInput = h("input", { type: "text", value: c.name, style: "max-width:20rem" });
  root.append(h("h2", {}, "Name"), h("div", { class: "row" }, nameInput, h("button", { type: "button", onclick: async () => {
    try { await api.patch(`/api/credentials/${id}`, { name: nameInput.value }); say("Renamed"); } catch (err) { say(describe(err), "error"); }
  } }, "Save name")));

  const replace = h("div", {});
  const secretInputs: Record<string, HTMLInputElement | HTMLTextAreaElement> = {};
  const addSecret = (key: string, label: string, el: HTMLInputElement | HTMLTextAreaElement) => { secretInputs[key] = el; replace.append(field(label, el)); };
  if (c.auth_kind === "api_key") { addSecret("key", "New API key", h("input", { type: "password", autocomplete: "off" })); addSecret("header", "Header name", h("input", { type: "text", value: String(meta.header ?? "X-API-Key") })); }
  if (c.auth_kind === "basic") { addSecret("username", "Username", h("input", { type: "text", value: String(meta.username ?? "") })); addSecret("password", "New password", h("input", { type: "password", autocomplete: "off" })); }
  if (c.auth_kind === "bearer") addSecret("token", "New token", h("input", { type: "password", autocomplete: "off" }));
  if (c.auth_kind === "service_account") addSecret("json", "New service account JSON", h("textarea", {}));
  if (c.auth_kind === "oauth2") { addSecret("client_id", "Client ID", h("input", { type: "text", value: String(meta.client_id ?? "") })); addSecret("client_secret", "New client secret", h("input", { type: "password", autocomplete: "off" })); }
  root.append(h("h2", {}, "Replace secret"), h("p", { class: "muted" }, "The current value is never shown. Enter a new one to replace it."), replace,
    h("div", { class: "row" }, h("button", { type: "button", onclick: async () => {
      const values: Record<string, unknown> = {}; for (const [k, el] of Object.entries(secretInputs)) values[k] = el.value;
      try { await api.patch(`/api/credentials/${id}`, { values }); say("Secret replaced"); for (const el of Object.values(secretInputs)) if (el.type === "password" || el.tagName === "TEXTAREA") el.value = ""; }
      catch (err) { say(describe(err), "error"); }
    } }, "Replace")));

  const users = toolsets.filter((t) => t.credential_id === c.id).map((t) => t.key);
  root.append(h("div", { class: "danger-zone" }, h("h2", { style: "margin-top:0" }, "Danger zone"),
    users.length ? h("p", {}, "In use by ", users.join(", "), ". Detach it from those toolsets before deleting.") : h("p", {}, "Not used by any toolset."),
    h("button", { type: "button", class: "danger", disabled: users.length > 0, onclick: async () => {
      if (!confirmDialog(`Delete credential ${c.name}? This cannot be undone.`)) return;
      try { await api.del(`/api/credentials/${id}`); navigate("/credentials"); } catch (err) { say(describe(err), "error"); }
    } }, "Delete credential")));
};
