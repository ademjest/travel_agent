import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { createHash } from "node:crypto";
import { readFileSync, readdirSync } from "node:fs";
import { fileURLToPath } from "node:url";

const sourceRoot = fileURLToPath(new URL("./src/", import.meta.url));
const buildHash = createHash("sha256");
for (const name of readdirSync(sourceRoot).sort()) {
  if (/\.(tsx?|css)$/.test(name)) buildHash.update(readFileSync(sourceRoot + name));
}

export default defineConfig({
  define: { __FRONTEND_BUILD_ID__: JSON.stringify(buildHash.digest("hex").slice(0, 12)) },
  plugins: [react()],
  server: {
    proxy: {
      "/api": "http://127.0.0.1:8080",
      "/health": "http://127.0.0.1:8080",
    },
  },
});
