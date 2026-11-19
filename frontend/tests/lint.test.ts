// @vitest-environment node
import { spawnSync } from "node:child_process";
import { existsSync, mkdirSync, rmSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { afterEach, describe, expect, it } from "vitest";

const frontend = join(dirname(fileURLToPath(import.meta.url)), "..");

// Bundled JS that breaks a lint rule, like the trace viewer Playwright writes into its report.
const BAD = "export function f(x) { if (x) { useState(0); } }\nfunction useState(v) { return v; }\n";

const created: string[] = [];

afterEach(() => {
  for (const dir of created.splice(0)) rmSync(dir, { recursive: true, force: true });
});

function plant(dir: string) {
  const root = join(frontend, dir);
  const marker = join(root, "lint-regression");
  created.push(existsSync(root) ? marker : root);
  mkdirSync(marker, { recursive: true });
  writeFileSync(join(marker, "bad.js"), BAD);
}

describe("frontend lint", () => {
  // A failed `make e2e` leaves these output folders behind; `make test` must still pass.
  it.each(["playwright-report", "test-results"])("ignores %s/", (dir) => {
    plant(dir);
    const result = spawnSync(process.execPath, [join(frontend, "node_modules/eslint/bin/eslint.js")], {
      cwd: frontend,
      encoding: "utf8",
    });
    expect(result.stdout + result.stderr).not.toContain("lint-regression");
    expect(result.status).toBe(0);
  }, 120_000);
});
