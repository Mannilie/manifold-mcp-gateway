import { api, describe, type Credential, type ToolsetDetail } from "../api";
import { clear, h } from "../dom";
import { navigate, setDirty, type Page } from "../router";

const UPSTREAM_KINDS = ["none", "api_key", "bearer", "basic"];

export const addProxyPage: Page = async (root) => {
  const credentials = (await api.get<Credential[]>("/api/credentials")).filter((c) => UPSTREAM_KINDS.includes(c.auth_kind));
  clear(root);
  const key = h("input", { type: "text", placeholder: "unraid", pattern: "[a-z0-9-]+", oninput: () => setDirty("Leave without creating the toolset?") });
  const displayName = h("input", { type: "text", placeholder: "Unraid" });
  const url = h("input", { type: "url", placeholder: "http://unraid-mcp:6970/mcp" });
  const prefix = h("input", { type: "text", placeholder: "optional, for example unraid_" });
  const cred = h("select", {}, h("option", { value: "" }, "No authentication"), ...credentials.map((c) => h("option", { value: String(c.id) }, `${c.name} (${c.auth_kind})`)));
  const notice = h("p", {});
  const say = (text: string, cls = "muted") => { notice.textContent = text; notice.className = cls; };
  const toolsBox = h("div", {});
  let discovered: { name: string; description: string }[] = [];
  const choices: Record<string, HTMLSelectElement> = {};

  const discoverBtn = h("button", { type: "button", onclick: async () => {
    discoverBtn.disabled = true; say("Asking the upstream for its tools");
    try {
      discovered = await api.post(`/api/proxy/discover`, { upstream_url: url.value, credential_id: cred.value ? Number(cred.value) : null });
      toolsBox.replaceChildren();
      if (discovered.length === 0) toolsBox.append(h("p", { class: "muted" }, "The upstream reports no tools."));
      const table = h("table", {}, h("thead", {}, h("tr", {}, h("th", {}, "Tool"), h("th", {}, "Description"), h("th", {}, "Rule"))));
      for (const t of discovered) {
        const sel = h("select", {}, h("option", { value: "allow" }, "Allow"), h("option", { value: "deny" }, "Deny"));
        choices[t.name] = sel;
        table.append(h("tr", {}, h("td", {}, h("code", {}, t.name)), h("td", { class: "muted" }, t.description), h("td", {}, sel)));
      }
      toolsBox.append(h("p", { class: "muted" }, "Allow everything and deny a few, or the reverse. If every tool is allowed the allow list is left empty, which means any tool not denied."), table);
      say(`${discovered.length} tools found`);
    } catch (err) { say(describe(err), "error"); } finally { discoverBtn.disabled = false; }
  } }, "Discover tools");

  const create = h("button", { type: "button", class: "primary", onclick: async () => {
    const denied = Object.entries(choices).filter(([, s]) => s.value === "deny").map(([n]) => n);
    const allowed = Object.entries(choices).filter(([, s]) => s.value === "allow").map(([n]) => n);
    const allow = denied.length > 0 && allowed.length > 0 && denied.length < allowed.length ? [] : allowed.length > 0 && denied.length > 0 ? allowed : [];
    create.disabled = true;
    try {
      const t = await api.post<ToolsetDetail>("/api/toolsets", { key: key.value.trim(), display_name: displayName.value.trim(), upstream_url: url.value.trim(), prefix: prefix.value.trim() || null, credential_id: cred.value ? Number(cred.value) : null, allow, deny: denied });
      setDirty(null);
      navigate(`/toolsets/${t.key}`);
    } catch (err) { say(describe(err), "error"); create.disabled = false; }
  } }, "Create toolset");

  root.append(
    h("p", {}, h("a", { href: "/", "data-link": true }, "← Toolsets")),
    h("h1", {}, "Add proxy toolset"),
    h("p", { class: "muted" }, "A proxy toolset re-exports an upstream MCP server. It is created disabled. Proxy serving arrives in Phase 5; until then the configuration is stored and shown."),
    h("label", { class: "field" }, h("span", {}, "Key"), key, h("small", {}, "Lowercase letters, digits and hyphens. Permanent once a connector is registered.")),
    h("label", { class: "field" }, h("span", {}, "Display name"), displayName),
    h("label", { class: "field" }, h("span", {}, "Upstream URL"), url),
    h("label", { class: "field" }, h("span", {}, "Upstream authentication"), cred, h("small", {}, "Only api_key, bearer and basic credentials can authenticate to an upstream. ", h("a", { href: "/credentials", "data-link": true }, "Add one"))),
    h("label", { class: "field" }, h("span", {}, "Tool prefix"), prefix),
    h("div", { class: "row" }, discoverBtn),
    toolsBox,
    h("div", { class: "row" }, create),
    notice,
  );
};
