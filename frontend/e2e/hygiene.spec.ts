import { execFileSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import path from "node:path";
import { expect, test } from "@playwright/test";
import { DEMO_USER } from "../src/components/sites/claude-shell/data";

// Repository checks for the frontend; they read files and need no browser.
const root = path.resolve(__dirname, "..");
const tracked = () =>
  execFileSync("git", ["ls-files", "-z"], { cwd: root, encoding: "utf8" }).split("\0").filter(Boolean);

test("the cloner template's leftovers are gone", () => {
  const gone = [
    ".github",
    ".agents",
    // Not the whole .claude directory: a gitignored .claude/settings.local.json is allowed.
    ".claude/commands/clone-website.md",
    "docs/assets",
    "docs/design-references",
    "README.md",
    "CHANGELOG.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "Dockerfile",
    "Dockerfile.dev",
    "docker-compose.yml",
    ".dockerignore",
    "src/components/sites/claude-shell/PartsDrawing.tsx",
  ];
  for (const p of gone) expect(existsSync(path.join(root, p)), p).toBe(false);
  expect(tracked().filter((f) => f.endsWith(".gitkeep"))).toEqual([]);

  const pkg = JSON.parse(readFileSync(path.join(root, "package.json"), "utf8"));
  expect(JSON.stringify(pkg)).not.toMatch(/clone|JCodesMore|website/i);
  for (const key of ["author", "repository", "homepage", "bugs", "keywords"]) expect(pkg[key], key).toBeUndefined();

  // Agent files hold only Next's managed block and a pointer to the root rules.
  for (const f of ["AGENTS.md", "CLAUDE.md"]) {
    const text = readFileSync(path.join(root, f), "utf8");
    expect(text, f).not.toMatch(/Vercel|clone-website|Real content|Reverse-Engineer/i);
  }
});

test("no personal data in code, fixtures or docs", () => {
  expect(DEMO_USER.name).toBe("Demo user");
  // Reserved test domains (RFC 2606: .example, .test, .invalid) are nobody's address.
  const email = /[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+\.(?!(?:example|test|invalid)\b)[A-Za-z]{2,}/;
  const homeDir = /\/(Users|home)\/[a-z][\w.-]+\//;
  const textFiles = tracked().filter(
    (f) => /\.(tsx?|mts|mjs|json|md|css|ya?ml)$/.test(f) && f !== "package-lock.json",
  );
  const hits: string[] = [];
  for (const f of textFiles) {
    readFileSync(path.join(root, f), "utf8")
      .split("\n")
      .forEach((line, i) => {
        // npm package scopes such as @types/node are not emails.
        const stripped = line.replace(/@[a-z0-9-]+\/[a-z0-9.-]+/g, "");
        if (email.test(stripped) || homeDir.test(stripped)) hits.push(`${f}:${i + 1}: ${line.trim()}`);
      });
  }
  expect(hits).toEqual([]);
});

test("the welcome logo is the press-kit asset, with its source noted and no hand-drawn stand-in", () => {
  const logo = "public/claude-logo.svg";
  expect(tracked()).toContain(logo);
  const svg = readFileSync(path.join(root, logo), "utf8");
  expect(svg).toMatch(/^<svg[\s>]/);
  expect(svg).not.toMatch(/<script|<image|href=/i);

  // No component named Spark (the old hand-drawn mark) and no imports of it.
  expect(existsSync(path.join(root, "src/components/sites/claude-shell/Spark.tsx"))).toBe(false);
  const sources = tracked().filter((f) => f.startsWith("src/") && /\.tsx?$/.test(f));
  const uses = sources.filter((f) => /\bSpark\b/.test(readFileSync(path.join(root, f), "utf8")));
  expect(uses).toEqual([]);

  const readme = readFileSync(path.join(root, "..", "README.md"), "utf8");
  expect(readme).toContain("frontend/public/claude-logo.svg");
  expect(readme).toContain("https://www.anthropic.com/press-kit");
});
