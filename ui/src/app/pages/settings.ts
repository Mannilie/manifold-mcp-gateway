import { api, describe, type RestoreReport, type Settings, type Snapshot } from "../api";
import { clear, confirmDialog, confirmTyped, fmtTime, h } from "../dom";
import type { Page } from "../router";

function backupsTable(backups: Snapshot[]): HTMLElement {
  if (backups.length === 0) return h("p", { class: "muted" }, "No snapshots yet.");
  const table = h("table", {}, h("thead", {}, h("tr", {}, h("th", {}, "Snapshot"), h("th", {}, "Reason"), h("th", {}, "Taken"), h("th", {}, "Size"))));
  for (const b of backups) {
    table.append(h("tr", {}, h("td", {}, h("a", { href: `/api/backups/${b.name}` }, b.name)), h("td", {}, b.reason), h("td", {}, fmtTime(b.created_at)), h("td", {}, `${(b.size_bytes / 1024).toFixed(0)} KB`)));
  }
  return table;
}

function restorePanel(say: (text: string, cls?: string) => void): HTMLElement {
  const file = h("input", { type: "file", accept: ".db,application/vnd.sqlite3,application/octet-stream" });
  const report = h("div", {});
  const validate = h("button", { type: "button", onclick: async () => {
    const f = file.files?.[0];
    if (!f) { say("Choose a snapshot file first.", "error"); return; }
    validate.disabled = true; report.replaceChildren(h("p", { class: "muted" }, "Validating"));
    try {
      const form = new FormData(); form.append("file", f);
      const res = await fetch("/api/backups/restore/validate", { method: "POST", body: form, headers: { "X-Manifold-Request": "1" }, credentials: "same-origin" });
      const body = await res.json();
      if (!res.ok) { report.replaceChildren(h("p", { class: "error" }, typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail))); return; }
      report.replaceChildren(reportView(body as RestoreReport, say));
    } catch (err) { report.replaceChildren(h("p", { class: "error" }, describe(err))); } finally { validate.disabled = false; }
  } }, "Upload and validate");
  return h("div", { class: "danger-zone" },
    h("h2", { style: "margin-top:0" }, "Restore"),
    h("p", {}, "Two steps. Upload a snapshot and read the comparison, then confirm. The live database is snapshotted first, so a bad restore is reversible from the Backups list."),
    h("div", { class: "row" }, file, validate),
    report,
  );
}

function reportView(r: RestoreReport, say: (text: string, cls?: string) => void): HTMLElement {
  const keys = Array.from(new Set([...Object.keys(r.live_counts), ...Object.keys(r.counts)]));
  const table = h("table", {}, h("thead", {}, h("tr", {}, h("th", {}, ""), h("th", {}, "Live"), h("th", {}, "Snapshot"))),
    h("tr", {}, h("td", {}, "Schema version"), h("td", {}, String(r.live_schema_version)), h("td", {}, String(r.schema_version))),
    h("tr", {}, h("td", {}, "Snapshot first booted"), h("td", {}, ""), h("td", {}, fmtTime(r.snapshot_created_at))),
    ...keys.map((k) => h("tr", {}, h("td", {}, k), h("td", {}, String(r.live_counts[k] ?? "")), h("td", {}, String(r.counts[k] ?? "")))),
  );
  const confirm = h("button", { type: "button", class: "danger", onclick: async () => {
    if (!confirmTyped("Replace the live database with this snapshot? The gateway restarts and every connector keeps working only if the snapshot holds their clients.", "restore")) return;
    confirm.disabled = true;
    try { const res = await api.post<{ pre_restore: string }>("/api/backups/restore/confirm", { token: r.token }); say(`Restore staged. Live copy kept as ${res.pre_restore}. Restarting, this page reloads in a few seconds.`); setTimeout(() => location.reload(), 8000); }
    catch (err) { say(describe(err), "error"); confirm.disabled = false; }
  } }, "Confirm restore");
  return h("div", {}, table, r.warnings.length ? h("ul", {}, ...r.warnings.map((w) => h("li", { class: "error" }, w))) : h("p", { class: "muted" }, "No warnings."), h("div", { class: "row" }, confirm));
}

export const settingsPage: Page = async (root) => {
  const s = await api.get<Settings>("/api/settings");
  clear(root);
  const notice = h("p", {});
  const say = (text: string, cls = "muted") => { notice.textContent = text; notice.className = cls; };
  const level = h("select", {}, ...["debug", "info", "warning", "error"].map((l) => h("option", { value: l }, l)));
  level.value = s.log_level;
  const retention = h("input", { type: "number", min: 1, max: 365, value: String(s.audit_retention_days), style: "max-width:8rem" });
  const cap = h("input", { type: "number", min: 10000, max: 1000000, step: 1000, value: String(s.audit_row_cap), style: "max-width:10rem" });
  const backups = await api.get<Snapshot[]>("/api/backups");
  root.append(
    h("h1", {}, "Settings"),
    h("h2", {}, "Master key"),
    h("p", {}, s.master_key.verified ? `Verified against the key check written ${fmtTime(s.master_key.first_run_at)}.` : "No key check row yet."),
    h("p", { class: "muted" }, "Losing MANIFOLD_MASTER_KEY loses every stored credential. Keep it in a password manager."),
    h("h2", {}, "Logging and retention"),
    h("label", { class: "field" }, h("span", {}, "Log level"), level, h("small", {}, `Currently from the ${s.log_level_source}. Saving stores it in the database, which overrides the environment.`)),
    h("label", { class: "field" }, h("span", {}, "Audit retention, days"), retention, h("small", {}, "1 to 365. Older rows are pruned daily in small batches.")),
    h("label", { class: "field" }, h("span", {}, "Audit row cap"), cap, h("small", {}, "10,000 to 1,000,000. Oldest rows go first when the cap is hit.")),
    h("p", { class: "muted" }, `${s.audit.rows.toLocaleString()} audit rows, oldest ${fmtTime(s.audit.oldest_ts)}.`,
      s.audit.last_prune ? ` Last prune ${fmtTime(s.audit.last_prune.ts)}: ${s.audit.last_prune.by_age} by age` : " Never pruned.",
      s.audit.last_prune && s.audit.last_prune.by_cap > 0 ? h("span", { class: "error" }, `, ${s.audit.last_prune.by_cap} pruned by cap`) : null),
    h("div", { class: "row" }, h("button", { type: "button", class: "primary", onclick: async () => {
      try { await api.patch("/api/settings", { log_level: level.value, audit_retention_days: Number(retention.value), audit_row_cap: Number(cap.value) }); say("Saved"); } catch (err) { say(describe(err), "error"); }
    } }, "Save"), h("button", { type: "button", onclick: async () => {
      try { const r = await api.post<{ by_age: number; by_cap: number; rows: number }>("/api/settings/prune-audit"); say(`Pruned ${r.by_age} by age, ${r.by_cap} by cap. ${r.rows} rows remain.`); } catch (err) { say(describe(err), "error"); }
    } }, "Prune now")),
    notice,
    h("h2", {}, "Backups"),
    h("p", { class: "muted" }, "A snapshot is taken daily, before every migration and before every restore. The newest 14 are kept under /data/backups. Credentials inside are encrypted; a restore needs the same master key."),
    backupsTable(backups),
    h("div", { class: "row" }, h("button", { type: "button", onclick: async () => {
      try { await api.post("/api/backups"); say("Snapshot written"); location.reload(); } catch (err) { say(describe(err), "error"); }
    } }, "Snapshot now")),
    restorePanel(say),
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
