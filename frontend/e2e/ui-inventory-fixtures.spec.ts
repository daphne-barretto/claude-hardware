import { readFileSync } from "node:fs";
import path from "node:path";
import { expect, test, type Page } from "@playwright/test";
import {
  checkHygiene,
  collectControls,
  compareCoverage,
  loadInventory,
  parseControls,
  preparePage,
  reachState,
  runBehaviour,
  type Inventory,
} from "./inventory/ui-inventory";

// Proves the UI inventory checks (ui-inventory.spec.ts) fail when they should, using
// deliberately broken fixture pages served from e2e/fixtures/ui-inventory. The pages are
// routed in the browser, so this also runs against staging.
const dir = path.join(__dirname, "fixtures/ui-inventory");
const load = (name: string) => loadInventory(path.join(dir, name));

async function serveFixtures(page: Page) {
  await page.route("**/__ui-inventory-fixtures/*.html", (route) => {
    const file = path.basename(new URL(route.request().url()).pathname);
    return route.fulfill({ contentType: "text/html", body: readFileSync(path.join(dir, file), "utf8") });
  });
}

async function coverage(page: Page, inv: Inventory, stateId: string) {
  const state = inv.states.find((s) => s.id === stateId)!;
  await reachState(page, inv, state);
  const controls = await collectControls(page);
  return { coverage: compareCoverage(inv, state, controls), hygiene: await checkHygiene(page, inv, state, controls) };
}

async function behaviour(page: Page, context: Parameters<typeof preparePage>[1], inv: Inventory, id: string) {
  const errors = await preparePage(page, context, inv);
  return runBehaviour(page, inv, inv.entries.find((e) => e.id === id)!, errors, 1_000);
}

test.beforeEach(async ({ page }) => serveFixtures(page));

test("a page that matches its inventory passes coverage and behaviour", async ({ page, context }) => {
  const inv = load("controls.yaml");
  expect(await coverage(page, inv, "good")).toEqual({ coverage: [], hygiene: [] });
  for (const entry of inv.entries) expect(await behaviour(page, context, inv, entry.id)).toEqual([]);
});

test("coverage fails when a control is added without an inventory entry", async ({ page }) => {
  const { coverage: problems } = await coverage(page, load("controls.yaml"), "extra");
  expect(problems).toEqual(['extra: button "Archive" is not in the inventory']);
});

test("coverage fails when an inventory entry's control disappears", async ({ page }) => {
  const { coverage: problems } = await coverage(page, load("controls.yaml"), "missing");
  expect(problems).toEqual(['missing: inventory entry done (link "Done") no longer exists']);
});

test("behaviour fails on a dead button, even when its expected result is too weak to notice", async ({ page, context }) => {
  const inv = load("dead-button.yaml");
  const opener = await behaviour(page, context, inv, "more-models");
  expect(opener).toContainEqual(expect.stringMatching(/^more-models: expected \{"visible":"menu"\}/));
  expect(opener).toContain("more-models: no observable effect");
  // The Refresh entry's only expectation holds, so only the effect check catches it.
  expect(await behaviour(page, context, inv, "refresh")).toEqual(["refresh: no observable effect"]);
});

test("behaviour fails on a console error and on an unhandled rejection", async ({ page, context }) => {
  const inv = load("errors.yaml");
  const save = await behaviour(page, context, inv, "save");
  expect(save).toEqual(["save: console error: save failed"]);
  const loadErrors = await behaviour(page, context, inv, "load");
  expect(loadErrors.length).toBeGreaterThan(0);
  expect(loadErrors.every((e) => /load failed/.test(e))).toBe(true);
  expect(loadErrors).toContainEqual(expect.stringMatching(/unhandled rejection: load failed|page error: load failed/));
});

test("hygiene fails on a disabled control that neither looks disabled nor says why", async ({ page }) => {
  const { coverage: problems, hygiene } = await coverage(page, load("dead-button.yaml"), "dead");
  expect(problems).toEqual([]);
  expect(hygiene).toEqual([
    'dead: disabled button "Export" does not look disabled',
    'dead: disabled button "Export" does not say why it is disabled',
  ]);
});

test("nth picks one of several same-named controls, focus reaches a covered one, download checks the file", async ({
  page,
  context,
}) => {
  const inv = load("keyboard-download.yaml");
  expect(await coverage(page, inv, "page")).toEqual({ coverage: [], hygiene: [] });
  for (const id of ["remove", "covered", "report"]) expect(await behaviour(page, context, inv, id)).toEqual([]);
});

test("behaviour fails when a download isn't named like its control", async ({ page, context }) => {
  const problems = await behaviour(page, context, load("keyboard-download.yaml"), "notes");
  expect(problems).toEqual([expect.stringMatching(/^notes: expected \{"download":true\}/)]);
});

test("coverage sees controls whose names have a colon, which the ARIA snapshot quotes", () => {
  const snapshot = [
    '- region "Parts":',
    "  - list:",
    "    - listitem:",
    "      - 'button \"Hide: base\" [pressed]'",
    "      - 'button \"It''s: lid\"': lid",
    '      - button "Select base": base',
  ].join("\n");
  expect(parseControls(snapshot).map((c) => [c.role, c.name, c.ancestors.map((a) => a.role).join(">")])).toEqual([
    ["button", "Hide: base", "region>list>listitem"],
    ["button", "It's: lid", "region>list>listitem"],
    ["button", "Select base", "region>list>listitem"],
  ]);
});
