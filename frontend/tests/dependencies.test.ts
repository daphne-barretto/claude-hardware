// @vitest-environment node
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { describe, expect, it } from "vitest";

const require = createRequire(import.meta.url);

function installed(name: string) {
  return (JSON.parse(readFileSync(require.resolve(`${name}/package.json`), "utf8")) as { version: string }).version;
}

function atLeast(version: string, min: string) {
  const a = version.split(/[.-]/).slice(0, 3).map(Number);
  const b = min.split(".").map(Number);
  for (let i = 0; i < 3; i++) if (a[i] !== b[i]) return a[i] > b[i];
  return true;
}

describe("dependencies", () => {
  // next <= 16.4.0 has unauthenticated RCE advisories (GHSA-2464-fhr4-w9xw, GHSA-673q-mjm6-r5xr,
  // GHSA-vcvr-r3jv-pc5j); 16.4.6 is the first release clear of every production advisory.
  it("runs a patched Next.js", () => {
    expect(atLeast(installed("next"), "16.4.6")).toBe(true);
  });

  it("keeps the shadcn CLI out of runtime dependencies", () => {
    const pkg = JSON.parse(readFileSync(new URL("../package.json", import.meta.url), "utf8")) as {
      dependencies: Record<string, string>;
    };
    expect(pkg.dependencies).not.toHaveProperty("shadcn");
  });
});
