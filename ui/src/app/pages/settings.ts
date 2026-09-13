import { api, describe, type Settings } from "../api";
import { clear, confirmDialog, fmtTime, h } from "../dom";
import type { Page } from "../router";

export const settingsPage: Page = async (root) => {
  const s = await api.get<Settings>("/api/settings");
  clear(root);
  const notice = h("p", {});
  const say = (text: string, cls = "muted") => { notice.textContent = text; notice.className = cls; };
  const level = h("select", {}, ...["debug", "info", "warning", "error"].map((l) => h("option", { value: l }, l)));
  level.value = s.log_level;
  const retention = h("input", { type: "number", min: 1, max: 3650, value: String(s.audit_retention_days), style: "max-width:8rem" });
  root.append(
    h("h1", {}, "Settings"),
    h("h2", {}, "Master key"),
    h("p", {}, s.master_key.verified ? `Verified against the key check written ${fmtTime(s.master_key.first_run_at)}.` : "No key check row yet."),
    h("p", { class: "muted" }, "Losing MANIFOLD_MASTER_KEY loses every stored credential. Keep it in a password manager."),
    h("h2", {}, "Logging and retention"),
    h("label", { class: "field" }, h("span", {}, "Log level"), level, h("small", {}, `Currently from the ${s.log_level_source}. Saving stores it in the database, which overrides the environment.`)),
    h("label", { class: "field" }, h("span", {}, "Audit retention, days"), retention, h("small", {}, "Pruning arrives in Phase 6. The value is stored now.")),
    h("div", { class: "row" }, h("button", { type: "button", class: "primary", onclick: async () => {
      try { await api.patch("/api/settings", { log_level: level.value, audit_retention_days: Number(retention.value) }); say("Saved"); } catch (err) { say(describe(err), "error"); }
    } }, "Save")),
    notice,
    h("h2", {}, "Export"),
    h("p", {}, h("a", { href: "/api/settings/export" }, "Download config as YAML"), h("span", { class: "muted" }, " with credentials redacted. Import is not supported.")),
    h("h2", {}, "Connectors"),
    h("p", {}, `${s.oauth_clients} claude.ai client${s.oauth_clients === 1 ? "" : "s"} registered.`),
    h("div", { class: "row" }, h("button", { type: "button", class: "danger", onclick: async () => {
      if (!confirmDialog("Disconnect every claude.ai connector? Each one must re-authorise.")) return;
      try { await api.post("/api/settings/disconnect-all"); say("All connectors disconnected"); } catch (err) { say(describe(err), "error"); }
    } }, "Disconnect all connectors")),
    h("h2", {}, "Gateway"),
    h("dl", { class: "kv" }, h("dt", {}, "Version"), h("dd", {}, s.version), h("dt", {}, "Schema"), h("dd", {}, String(s.schema_version)), h("dt", {}, "Base URL"), h("dd", {}, s.base_url), h("dt", {}, "Admins"), h("dd", {}, s.admin_emails.join(", "))),
    h("div", { class: "row" }, h("button", { type: "button", class: "danger", onclick: async () => {
      if (!confirmDialog("Restart the gateway? Tool calls in flight will fail. Connectors stay connected.")) return;
      try { await api.post("/api/settings/restart"); say("Restarting. This page will reload in a few seconds."); setTimeout(() => location.reload(), 6000); } catch (err) { say(describe(err), "error"); }
    } }, "Restart gateway")),
  );
};
