import { afterEach, expect, it } from "vitest";
import { mkdtempSync, mkdirSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { frontendBuildId } from "../vite.config";

const roots: string[] = [];
afterEach(() => { for (const root of roots.splice(0)) rmSync(root, { recursive: true, force: true }); });

it("includes nested production inputs and configuration, excluding tests and build output", () => {
  const root = mkdtempSync(join(tmpdir(), "travel-build-id-"));
  roots.push(root);
  const write = (name: string, text: string) => {
    const path = join(root, name);
    mkdirSync(join(path, ".."), { recursive: true });
    writeFileSync(path, text);
  };
  write("src/nested/widget.ts", "export const value = 1;");
  write("vite.config.ts", "export default {};");
  write("tsconfig.json", "{}");
  write("index.html", "<div id='root'></div>");
  let previous = frontendBuildId(root);
  for (const name of ["src/nested/widget.ts", "vite.config.ts", "tsconfig.json", "index.html", "public/icon.svg", "package-lock.json"]) {
    write(name, "changed");
    const next = frontendBuildId(root);
    expect(next).not.toBe(previous);
    previous = next;
  }
  write("src/nested/widget.test.ts", "test code");
  write("dist/index.html", "built output");
  expect(frontendBuildId(root)).toBe(previous);
});
