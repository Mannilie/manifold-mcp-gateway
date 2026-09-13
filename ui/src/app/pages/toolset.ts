import { api, describe, type Credential, type ToolsetDetail } from "../api";
import { clear, confirmDialog, copyButton, fmtTime, h } from "../dom";
import { navigate, setDirty, type Page } from "../router";
import { listEditor, schemaForm } from "../schemaForm";

const DIRTY = "You have unsaved changes on this toolset. Leave anyway?";

export const toolsetPage: Page = async (root, params) => {
  const key = params.key ?? "";
  const [t, credentials] = await Promise.all([
    api.get<ToolsetDetail>(`/api/toolsets/${key}`),
    api.get<Credential[]>("/api/credentials"),
  ]);
  clear(root);
  const notice = h("p", {});
  const say = (text: string, cls = "muted") => { notice.textContent = text; notice.className = cls; };

  async function patch(body: Record<string, unknown>, done = "Saved"): Promise<ToolsetDetail | null> {
    try {
      const updated = await api.patch<ToolsetDetail>(`/api/toolsets/${key}`, body);
      say(done);
      return updated;
    } catch (err) {
      say(describe(err), "error");
      return null;
    }
  }

  // Header
  const nameInput = h("input", { type: "text", value: t.display_name, style: "max-width:20rem" });
  const dot = h("span", { class: `dot ${t.health.status}` });
  const healthText = h("span", { class: "muted" }, t.health.detail || t.health.status);
  const testBtn = h("button", { type: "button", onclick: async () => {
    testBtn.disabled = true;
    try {
      const r = await api.post<{ status: string; detail: string }>(`/api/toolsets/${key}/test`);
      dot.className = `dot ${r.status}`;
      healthText.textContent = r.detail || r.status;
    } catch (err) { say(describe(err), "error"); } finally { testBtn.disabled = false; }
  } }, "Test connection");
  const enabled = h("input", { type: "checkbox", checked: t.enabled, disabled: key === "manifold" });
  enabled.addEventListener("change", () => void patch({ enabled: enabled.checked }, enabled.checked ? "Enabled" : "Disabled"));

  root.append(
    h("p", {}, h("a", { href: "/", "data-link": true }, "← Toolsets")),
    h("div", { class: "row" }, dot, h("h1", { style: "margin:0" }, t.display_name), h("span", { class: "tag" }, t.kind), t.version ? h("span", { class: "tag" }, `v${t.version}`) : null),
    h("div", { class: "row" }, h("label", { class: "switch" }, enabled, key === "manifold" ? "Always on" : "Enabled"), healthText, testBtn),
    h("div", {}, t.error ? h("p", { class: "error" }, "Last reload failed: ", t.error) : null),
    h("div", { class: "row" }, h("code", {}, t.endpoint_url), copyButton(t.endpoint_url), h("span", { class: "muted" }, "Last call ", fmtTime(t.last_call_at))),
    h("div", { class: "checklist" },
      h("div", {}, "Cloudflare Access bypass path: ", h("code", {}, t.cloudflare.bypass_path), " (covers ", t.cloudflare.covers.join(" and "), ")"),
      h("div", {}, "claude.ai connector URL: ", h("code", {}, t.cloudflare.connector_url)),
    ),
    notice,
  );

  // Display name
  root.append(h("h2", {}, "Name"), h("div", { class: "row" }, nameInput, h("button", { type: "button", onclick: () => void patch({ display_name: nameInput.value }) }, "Save name")));

  // Credential
  const usable = credentials.filter((c) => t.supported_auth.includes(c.auth_kind));
  const credSelect = h("select", { style: "max-width:24rem" }, h("option", { value: "" }, "None"), ...usable.map((c) => h("option", { value: String(c.id) }, `${c.name} (${c.auth_kind}${c.status === "ok" ? "" : ", " + c.status})`)));
  credSelect.value = t.credential_id === null ? "" : String(t.credential_id);
  root.append(
    h("h2", {}, "Credential"),
    h("p", { class: "muted" }, "Supports: ", t.supported_auth.join(", ")),
    h("div", { class: "row" },
      credSelect,
      h("button", { type: "button", onclick: () => void patch(credSelect.value === "" ? { detach_credential: true } : { credential_id: Number(credSelect.value) }) }, "Save credential"),
      h("a", { href: "/credentials", "data-link": true }, "Add new"),
    ),
  );

  // Settings
  const form = schemaForm(t.settings_schema, t.settings, () => setDirty(DIRTY));
  const saveSettings = h("button", { type: "button", class: "primary", onclick: async () => {
    const updated = await patch({ settings: form.value() }, "Settings saved");
    if (updated) { setDirty(null); form.reset(updated.settings); }
  } }, "Save settings");
  root.append(h("h2", {}, "Settings"), form.element, h("div", { class: "row" }, saveSettings));

  // Proxy upstream
  if (t.upstream) {
    const url = h("input", { type: "text", value: t.upstream.upstream_url });
    const prefix = h("input", { type: "text", value: t.upstream.prefix ?? "", placeholder: "optional tool name prefix" });
    const allow = listEditor(t.upstream.allow, "tool name", () => setDirty(DIRTY));
    const deny = listEditor(t.upstream.deny, "tool name", () => setDirty(DIRTY));
    root.append(
      h("h2", {}, "Upstream"),
      h("label", { class: "field" }, h("span", {}, "Upstream URL"), url),
      h("label", { class: "field" }, h("span", {}, "Tool prefix"), prefix),
      h("label", { class: "field" }, h("span", {}, "Allow list"), allow.element, h("small", {}, "Empty means every tool not on the deny list.")),
      h("label", { class: "field" }, h("span", {}, "Deny list"), deny.element),
      h("div", { class: "row" }, h("button", { type: "button", class: "primary", onclick: async () => {
        const updated = await patch({ upstream: { upstream_url: url.value, prefix: prefix.value || null, allow: allow.get(), deny: deny.get() } }, "Upstream saved");
        if (updated) setDirty(null);
      } }, "Save upstream")),
    );
  }

  // Tools
  root.append(h("h2", {}, "Tools"));
  if (t.tools.length === 0) root.append(h("p", { class: "muted" }, t.mounted ? "No tools." : "Enable the toolset to list its tools."));
  else {
    const table = h("table", {}, h("thead", {}, h("tr", {}, h("th", {}, "On"), h("th", {}, "Tool"), h("th", {}, "Description"))));
    const boxes: Record<string, HTMLInputElement> = {};
    for (const tool of t.tools) {
      const box = h("input", { type: "checkbox", checked: tool.enabled });
      boxes[tool.name] = box;
      box.addEventListener("change", () => {
        const disabled = Object.entries(boxes).filter(([, b]) => !b.checked).map(([n]) => n);
        void patch({ disabled_tools: disabled }, `${tool.name} ${box.checked ? "enabled" : "disabled"}`);
      });
      table.append(h("tr", {}, h("td", {}, box), h("td", {}, h("code", {}, tool.name)), h("td", { class: "muted" }, tool.description)));
    }
    root.append(table);
  }

  // Danger zone
  if (key !== "manifold") {
    const newKey = h("input", { type: "text", value: key, style: "max-width:16rem", pattern: "[a-z0-9-]+" });
    root.append(h("div", { class: "danger-zone" },
      h("h2", { style: "margin-top:0" }, "Danger zone"),
      h("p", {}, "Renaming changes the endpoint URL. The claude.ai connector for this toolset stops working until you add a new connector with the new URL and a new Access bypass rule."),
      h("div", { class: "row" }, newKey, h("button", { type: "button", class: "danger", onclick: async () => {
        if (newKey.value === key) return;
        if (!confirmDialog(`Rename ${key} to ${newKey.value}? The current connector will break.`)) return;
        try { await api.post(`/api/toolsets/${key}/rename`, { new_key: newKey.value }); navigate(`/toolsets/${newKey.value}`); }
        catch (err) { say(describe(err), "error"); }
      } }, "Rename")),
      h("p", { style: "margin-top:1rem" }, h("button", { type: "button", class: "danger", onclick: async () => {
        if (!confirmDialog(`Delete ${key}? This removes its configuration. It cannot be undone.`)) return;
        try { await api.del(`/api/toolsets/${key}`); navigate("/"); } catch (err) { say(describe(err), "error"); }
      } }, "Delete toolset")),
    ));
  }
};
