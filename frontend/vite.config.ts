import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { createHash } from "node:crypto";
import { existsSync, readFileSync, readdirSync } from "node:fs";
import { join, relative } from "node:path";
import { fileURLToPath } from "node:url";

export function frontendBuildId(root = fileURLToPath(new URL("./", import.meta.url))) {
  const files: string[] = [];
  function collect(directory: string) {
    if (!existsSync(directory)) return;
    for (const entry of readdirSync(directory, { withFileTypes: true })) {
      const path = join(directory, entry.name);
      if (entry.isDirectory() && entry.name !== "__tests__") collect(path);
      else if (entry.isFile() && !/\.(test|spec)\.[^.]+$/.test(entry.name)) files.push(path);
    }
  }
  collect(join(root, "src"));
  collect(join(root, "public"));
  for (const name of readdirSync(root)) {
    if (/^(?:vite\.config\.[cm]?[jt]s|tsconfig(?:\.[^.]+)?\.json|package(?:-lock)?\.json|index\.html)$/.test(name))
      files.push(join(root, name));
  }
  const hash = createHash("sha256");
  for (const path of files.sort((a, b) => relative(root, a).replaceAll("\\", "/").localeCompare(relative(root, b).replaceAll("\\", "/"), "en")))
    hash.update(relative(root, path).replaceAll("\\", "/") + "\0").update(readFileSync(path)).update("\0");
  return hash.digest("hex").slice(0, 12);
}

export default defineConfig({
  define: { __FRONTEND_BUILD_ID__: JSON.stringify(frontendBuildId()) },
  plugins: [react()],
  server: {
    proxy: {
      "/api": "http://127.0.0.1:8080",
      "/health": "http://127.0.0.1:8080",
    },
  },
});
