// Summarises a QA run (`make qa`) from what the tests wrote under test-results/qa/: the
// findings of every state and control, and Playwright's results. Writes
// test-results/qa/summary.md, a table of every finding.
// Runs with Node's own TypeScript support: `node e2e/qa/report.mts`.
import { existsSync, readdirSync, readFileSync, statSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";
// Node runs this file as is (type stripping), so the import names the .ts file itself.
// @ts-expect-error TS5097: allowImportingTsExtensions is off for the app
import { ownerOf } from "./known.ts";

type Finding = { where: string; kind: string; detail: string; owner?: string; screenshot?: string };
type Doc = {
  findings: Finding[];
  state?: string;
  entry?: string;
  viewport?: string;
  what?: string;
  results?: { control: string; entry?: string; effect: string; problems: string[] }[];
};
type Spec = { title: string; file: string; tests: { status: string; results: { status: string; duration: number; error?: { message?: string } }[] }[] };
type Suite = { specs?: Spec[]; suites?: Suite[] };

const out = resolve(import.meta.dirname, "../../test-results/qa");
const dir = join(out, "findings");
const docs: Record<string, Doc> = {};
for (const f of existsSync(dir) ? readdirSync(dir) : []) docs[f.replace(/\.json$/, "")] = JSON.parse(readFileSync(join(dir, f), "utf8"));

// Playwright's results: every results*.json of the run.
// A rerun (`--last-failed`, QA_PART=rerun) writes its own file; its results replace the
// earlier ones for the tests it ran.
const byTest = new Map<string, Spec>();
const walk = (s: Suite) => {
  for (const spec of s.specs ?? []) byTest.set(`${spec.file}\0${spec.title}`, spec);
  for (const c of s.suites ?? []) walk(c);
};
let duration = 0;
const files = readdirSync(out)
  .filter((n) => /^results.*\.json$/.test(n))
  .sort((a, b) => statSync(join(out, a)).mtimeMs - statSync(join(out, b)).mtimeMs);
for (const f of files) {
  const r = JSON.parse(readFileSync(join(out, f), "utf8")) as { suites: Suite[]; stats?: { duration?: number } };
  r.suites.forEach(walk);
  if (f === "results.json") duration = r.stats?.duration ?? 0;
}
const specs = [...byTest.values()];
const outcome = (s: Spec) => s.tests[0]?.status ?? "skipped"; // expected, unexpected, flaky, skipped
const count = (pred: (s: Spec) => boolean) => specs.filter(pred).length;

const lines: string[] = [];
const p = (s = "") => lines.push(s);
p("# QA run summary");
p();
p(`Tests: ${specs.length}; passed ${count((s) => outcome(s) === "expected" && s.tests[0].results[0]?.status === "passed")}, ` +
  `failed ${count((s) => outcome(s) === "unexpected")}, expected failures (owned) ${count((s) => outcome(s) === "expected" && s.tests[0].results[0]?.status === "failed")}, ` +
  `flaky ${count((s) => outcome(s) === "flaky")}, skipped ${count((s) => outcome(s) === "skipped")}. Wall time of the full run ${(duration / 60000).toFixed(1)} min.`);
p();

// Owners as known.ts says now, so a rule added after the run applies to its findings.
for (const d of Object.values(docs)) for (const f of d.findings) f.owner = ownerOf(f);
const all = Object.values(docs).flatMap((d) => d.findings);
/** Owned by an in-flight branch; a "fix brief" owner is a failure with a fix to make. */
const owned = (f: Finding) => !!f.owner && !f.owner.startsWith("fix brief");
const byKind = new Map<string, { open: number; owned: number }>();
for (const f of all) {
  const k = byKind.get(f.kind) ?? { open: 0, owned: 0 };
  if (owned(f)) k.owned++;
  else k.open++;
  byKind.set(f.kind, k);
}
p("## Findings by kind");
p();
p("| Kind | Open | Owned by an in-flight branch |");
p("| --- | --- | --- |");
for (const [k, v] of [...byKind].sort()) p(`| ${k} | ${v.open} | ${v.owned} |`);
p();

// Unique findings (the same problem seen in several states is listed once, with where).
const unique = new Map<string, { f: Finding; where: Set<string> }>();
for (const f of all) {
  const key = `${f.kind}\0${f.detail.replace(/session-\d+|build-[0-9a-f]+|\d+(\.\d+)?/g, "#")}\0${f.owner ?? ""}`;
  const u = unique.get(key) ?? { f, where: new Set<string>() };
  u.where.add(f.where);
  unique.set(key, u);
}
for (const isOwnedTable of [false, true]) {
  p(isOwnedTable ? "## Owned findings" : "## Open findings (the Owner column names a fix brief where there is one)");
  p();
  p("| Kind | Finding | Where | Owner | Screenshot |");
  p("| --- | --- | --- | --- | --- |");
  for (const { f, where } of [...unique.values()].filter((u) => owned(u.f) === isOwnedTable).sort((a, b) => a.f.kind.localeCompare(b.f.kind)))
    p(`| ${f.kind} | ${f.detail.replace(/\|/g, "\\|").slice(0, 220)} | ${[...where].slice(0, 4).join(", ")}${where.size > 4 ? ` (+${where.size - 4})` : ""} | ${f.owner ?? ""} | ${f.screenshot ? `[png](../${f.screenshot})` : ""} |`);
  p();
}

// Every control the click sweep used, by state.
p("## Controls (click sweep)");
p();
p("| State | Control | Inventory entry | Effect | Result |");
p("| --- | --- | --- | --- | --- |");
for (const d of Object.values(docs).filter((x) => x.results))
  for (const r of d.results!)
    p(`| ${d.state} | ${r.control.replace(/\|/g, "\\|")} | ${r.entry ?? "(not in inventory)"} | ${r.effect.replace(/\|/g, "\\|").slice(0, 60)} | ${r.problems.length ? `fail: ${r.problems.join("; ").replace(/\|/g, "\\|").slice(0, 160)}` : "pass"} |`);
p();

// Every inventory entry on the real backend.
p("## Inventory entries on the real backend");
p();
p("| Test | Result | Note |");
p("| --- | --- | --- |");
for (const s of specs.filter((x) => x.file.endsWith("inventory.qa.ts"))) {
  const r = s.tests[0].results[0];
  const res = outcome(s) === "skipped" ? "skipped (mock only)" : outcome(s) === "expected" ? (r?.status === "failed" ? "owned (expected failure)" : "pass") : outcome(s) === "flaky" ? "flaky" : "fail";
  const note = res === "fail" ? (r?.error?.message ?? "").replace(/\u001b\[[0-9;]*m/g, "").split("\n").find((l) => /expected|failed|Timeout|Error/.test(l))?.trim().slice(0, 160) ?? "" : "";
  p(`| ${s.title.replace(/\|/g, "\\|")} | ${res} | ${note.replace(/\|/g, "\\|")} |`);
}

writeFileSync(join(out, "summary.md"), lines.join("\n") + "\n");
console.log(`QA summary: ${join(out, "summary.md")}`);
