import { api, type AuditEntry, type ToolsetSummary } from "../api";
import { clear, fmtTime, h } from "../dom";
import { navigate, type Page } from "../router";

const PAGE = 50;

export const auditPage: Page = async (root, _params, query) => {
  const toolsets = await api.get<ToolsetSummary[]>("/api/toolsets");
  const q = new URLSearchParams(query);
  q.set("limit", String(PAGE));
  const offset = Number(q.get("offset") ?? 0);
  const entries = await api.get<AuditEntry[]>(`/api/audit?${q.toString()}`);
  clear(root);
  const toolset = h("select", {}, h("option", { value: "" }, "All toolsets"), ...toolsets.map((t) => h("option", { value: t.key }, t.display_name)));
  toolset.value = query.get("toolset") ?? "";
  const tool = h("input", { type: "text", placeholder: "tool name", value: query.get("tool") ?? "" });
  const ok = h("select", {}, h("option", { value: "" }, "Ok and errors"), h("option", { value: "true" }, "Ok only"), h("option", { value: "false" }, "Errors only"));
  ok.value = query.get("ok") ?? "";
  const since = h("input", { type: "datetime-local", value: query.get("since")?.slice(0, 16) ?? "" });
  const until = h("input", { type: "datetime-local", value: query.get("until")?.slice(0, 16) ?? "" });
  const apply = (extra: Record<string, string> = {}) => {
    const p = new URLSearchParams();
    if (toolset.value) p.set("toolset", toolset.value);
    if (tool.value.trim()) p.set("tool", tool.value.trim());
    if (ok.value) p.set("ok", ok.value);
    if (since.value) p.set("since", new Date(since.value).toISOString());
    if (until.value) p.set("until", new Date(until.value).toISOString());
    for (const [k, v] of Object.entries(extra)) p.set(k, v);
    navigate(`/audit?${p.toString()}`);
  };
  root.append(
    h("h1", {}, "Audit log"),
    h("div", { class: "toolbar" }, toolset, tool, ok, since, until, h("button", { type: "button", onclick: () => apply() }, "Filter")),
    h("p", { class: "muted" }, "Arguments are stored as a hash only. The same arguments always produce the same hash."),
  );
  if (entries.length === 0) root.append(h("p", { class: "muted" }, "No calls match."));
  else {
    const table = h("table", {}, h("thead", {}, h("tr", {}, h("th", {}, "Time"), h("th", {}, "Toolset"), h("th", {}, "Tool"), h("th", {}, "Duration"), h("th", {}, "Result"), h("th", {}, "Args hash"))));
    for (const e of entries) {
      table.append(h("tr", {},
        h("td", {}, fmtTime(e.ts)), h("td", {}, e.toolset_key),
        h("td", {}, h("code", {}, e.tool_name), e.upstream_tool && e.upstream_tool !== e.tool_name ? h("span", { class: "muted" }, ` (upstream ${e.upstream_tool})`) : null),
        h("td", {}, `${e.duration_ms} ms`),
        h("td", { class: e.ok ? "" : "error" }, e.ok ? "ok" : e.error ?? "error"), h("td", { class: "mono muted", title: e.args_hash }, e.args_hash.slice(0, 12)),
      ));
    }
    root.append(table);
  }
  root.append(h("div", { class: "row", style: "margin-top:1rem" },
    h("button", { type: "button", disabled: offset === 0, onclick: () => apply({ offset: String(Math.max(0, offset - PAGE)) }) }, "Newer"),
    h("button", { type: "button", disabled: entries.length < PAGE, onclick: () => apply({ offset: String(offset + PAGE) }) }, "Older"),
  ));
};
