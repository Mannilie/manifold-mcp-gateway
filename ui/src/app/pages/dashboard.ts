import { api, describe, type ToolsetSummary } from "../api";
import { clear, copyButton, fmtTime, h } from "../dom";
import type { Page } from "../router";

export const dashboard: Page = async (root) => {
  const toolsets = await api.get<ToolsetSummary[]>("/api/toolsets");
  clear(root);
  root.append(
    h("div", { class: "row" },
      h("h1", {}, "Toolsets"),
      h("span", { style: "flex:1" }),
      h("a", { href: "/add-proxy", "data-link": true }, h("button", { type: "button" }, "Add proxy toolset")),
    ),
  );
  if (toolsets.length === 0) root.append(h("p", { class: "muted" }, "No toolsets."));
  const grid = h("div", { class: "cards" });
  for (const t of toolsets) grid.append(card(t));
  root.append(grid);
};

function card(t: ToolsetSummary): HTMLElement {
  const status = h("p", { class: "muted" });
  const toggle = h("input", { type: "checkbox", checked: t.enabled, disabled: t.key === "manifold" });
  toggle.addEventListener("change", async () => {
    toggle.disabled = true;
    status.textContent = toggle.checked ? "Enabling" : "Disabling";
    try {
      const updated = await api.patch<ToolsetSummary>(`/api/toolsets/${t.key}`, { enabled: toggle.checked });
      status.textContent = updated.mounted ? "Serving" : updated.enabled ? "Enabled, not mounted" : "Disabled";
      dot.className = `dot ${updated.health.status}`;
    } catch (err) {
      toggle.checked = !toggle.checked;
      status.textContent = describe(err);
      status.className = "error";
    } finally {
      toggle.disabled = t.key === "manifold";
    }
  });
  const dot = h("span", { class: `dot ${t.health.status}`, title: t.health.detail || t.health.status });
  return h("div", { class: "card" },
    h("h3", {}, dot, h("a", { href: `/toolsets/${t.key}`, "data-link": true }, t.display_name), h("span", { class: "tag" }, t.kind)),
    h("div", { class: "row" },
      h("label", { class: "switch" }, toggle, t.key === "manifold" ? "Always on" : "Enabled"),
      h("span", { class: "muted" }, `${t.tool_count} tool${t.tool_count === 1 ? "" : "s"}`),
      h("span", { class: "muted" }, "Last call ", fmtTime(t.last_call_at)),
    ),
    t.error ? h("p", { class: "error" }, t.error) : null,
    t.health.detail && !t.error ? h("p", { class: "muted" }, t.health.detail) : null,
    h("div", { class: "row" }, h("code", {}, t.endpoint_url), copyButton(t.endpoint_url)),
    h("div", { class: "checklist" },
      h("div", {}, "Cloudflare Access bypass path: ", h("code", {}, t.cloudflare.bypass_path)),
      h("div", { class: "muted" }, "Covers ", t.cloudflare.covers.join(" and ")),
      h("div", {}, "claude.ai connector URL: ", h("code", {}, t.cloudflare.connector_url)),
    ),
    status,
  );
}
