// History-based router with a dirty-form guard (DECISIONS.md, Phase 3 gate 2).

import { clear, h } from "./dom";
import { describe } from "./api";

export type Params = Record<string, string>;
export type Page = (root: HTMLElement, params: Params, query: URLSearchParams) => Promise<void>;

interface Route { pattern: RegExp; keys: string[]; page: Page }

const routes: Route[] = [];
let dirtyMessage: string | null = null;

export function route(path: string, page: Page): void {
  const keys: string[] = [];
  const pattern = new RegExp("^" + path.replace(/:(\w+)/g, (_, k) => { keys.push(k); return "([^/]+)"; }) + "/?$");
  routes.push({ pattern, keys, page });
}

export function setDirty(message: string | null): void {
  dirtyMessage = message;
}

export function isDirty(): boolean {
  return dirtyMessage !== null;
}

export function navigate(path: string, { replace = false } = {}): void {
  if (dirtyMessage && !window.confirm(dirtyMessage)) return;
  dirtyMessage = null;
  if (replace) history.replaceState(null, "", path); else history.pushState(null, "", path);
  void render();
}

export async function render(): Promise<void> {
  const root = document.getElementById("app") as HTMLElement;
  const url = new URL(location.href);
  const match = routes.map((r) => ({ r, m: r.pattern.exec(url.pathname) })).find((x) => x.m);
  document.querySelectorAll(".top nav a").forEach((a) => {
    const href = a.getAttribute("href") ?? "";
    a.classList.toggle("active", href === "/" ? url.pathname === "/" || url.pathname.startsWith("/toolsets") : url.pathname.startsWith(href));
  });
  clear(root);
  if (!match || !match.m) {
    root.append(h("h1", {}, "Not found"), h("p", {}, h("a", { href: "/", "data-link": true }, "Back to toolsets")));
    return;
  }
  const params: Params = {};
  match.r.keys.forEach((k, i) => (params[k] = decodeURIComponent(match.m![i + 1] ?? "")));
  root.append(h("p", { class: "muted" }, "Loading"));
  try {
    await match.r.page(root, params, url.searchParams);
  } catch (err) {
    clear(root);
    root.append(
      h("h1", {}, "Could not load this page"),
      h("p", { class: "error" }, describe(err)),
      h("p", {}, h("button", { type: "button", onclick: () => void render() }, "Try again")),
    );
  }
}

export function installLinkHandling(): void {
  document.addEventListener("click", (e) => {
    const a = (e.target as HTMLElement).closest("a[data-link]") as HTMLAnchorElement | null;
    if (!a || e.metaKey || e.ctrlKey || e.button !== 0) return;
    e.preventDefault();
    navigate(a.getAttribute("href") ?? "/");
  });
  window.addEventListener("popstate", () => {
    if (dirtyMessage && !window.confirm(dirtyMessage)) { history.go(1); return; }
    dirtyMessage = null;
    void render();
  });
  window.addEventListener("beforeunload", (e) => {
    if (dirtyMessage) { e.preventDefault(); }
  });
}
