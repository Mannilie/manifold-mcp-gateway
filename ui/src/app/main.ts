import { installLinkHandling, render, route } from "./router";
import { dashboard } from "./pages/dashboard";
import { toolsetPage } from "./pages/toolset";
import { credentialPage, credentialsPage } from "./pages/credentials";
import { addProxyPage } from "./pages/addProxy";
import { auditPage } from "./pages/audit";
import { settingsPage } from "./pages/settings";

export function start(): void {
  route("/", dashboard);
  route("/toolsets/:key", toolsetPage);
  route("/credentials", credentialsPage);
  route("/credentials/:id", credentialPage);
  route("/add-proxy", addProxyPage);
  route("/audit", auditPage);
  route("/settings", settingsPage);
  installLinkHandling();
  void render();
}
