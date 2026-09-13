import { defineConfig } from "astro/config";

// Static build served by FastAPI (DECISIONS.md, Phase 3 gate 1). One page shell; the app
// routes client-side and the server serves index.html for unknown paths.
export default defineConfig({
  output: "static",
  build: { format: "file" },
  vite: { build: { assetsInlineLimit: 0 } },
});
