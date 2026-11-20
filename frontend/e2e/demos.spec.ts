import { readFileSync } from "node:fs";
import path from "node:path";
import { expect, test } from "@playwright/test";
import { constraintItem, constraintPlaces, openConstraints } from "./fixtures/constraints";
import { locatedResults } from "../src/components/canvas/checks";
import type { CheckReport } from "../src/components/canvas/api";
import { openDemo, resetDemo } from "./fixtures/sessions";

// Demos: hardcoded saved sessions every browser sees in Recents, tagged Demo. Opening one gives
// the browser its own copy, a session like any other; the hardcoded original never changes.
// Served by the real backend and the mock alike, so this runs locally and against staging
// (E2E_BASE_URL) without Claude. Everything it expects comes from demos/sessions/, never from here.

const root = path.resolve(__dirname, "../../demos/sessions");
const read = (p: string) => JSON.parse(readFileSync(path.join(root, p), "utf8"));
const IDS = read("index.json").samples as string[];

type Doc = {
  session: { id: string; title: string; latest_build_id: string };
  messages: { role: string; text: string }[];
};
const DEMOS = IDS.map((id) => {
  const doc = read(`${id}/session.json`) as Doc;
  const build = doc.session.latest_build_id;
  const mesh = read(`${id}/builds/${build}/mesh.gltf`) as { nodes: { extras?: { part?: string } }[] };
  return {
    ...doc,
    build,
    parts: mesh.nodes.flatMap((n) => (n.extras?.part ? [n.extras.part] : [])),
    checks: read(`${id}/builds/${build}/checks.json`) as CheckReport,
  };
});

// What the chat shows first: the opening message, or, for a demo recorded before any message
// (the session as it was opened), its build's card.
const opening = (d: (typeof DEMOS)[number]) => d.messages[0]?.text ?? `${d.checks.summary.pass} pass`;

test("Recents list the demos after the user's sessions, each tagged Demo", async ({ page }) => {
  await page.goto("/");
  await expect(page.locator("[data-shell-ready=true]")).toBeAttached({ timeout: 30_000 });
  const recents = page.getByRole("list", { name: "Recents" });
  await expect(recents.locator("a[data-session]").first()).toBeVisible({ timeout: 30_000 });
  const listed = await recents.locator("a[data-session]").evaluateAll((links) =>
    links.map((l) => ({ id: l.getAttribute("data-session"), demo: !!l.querySelector("[data-testid=demo-tag]"), text: l.textContent ?? "" })),
  );
  const firstDemo = listed.findIndex((l) => l.demo);
  // The demos come last, in the catalogue's order; nothing after them is another session.
  expect(listed.slice(firstDemo).map((l) => l.id)).toEqual(IDS);
  expect(listed.slice(0, firstDemo).every((l) => !l.demo)).toBe(true);
  expect(listed.every((l) => !l.text.includes("Sample"))).toBe(true);
});

for (const demo of DEMOS) {
  test(`the demo "${demo.session.title}" opens as a session of the browser's own, with its conversation and its build fitted`, async ({ page }) => {
    const errors: string[] = [];
    page.on("pageerror", (err) => errors.push(err.message));
    await page.goto("/");
    await expect(page.locator("[data-shell-ready=true]")).toBeAttached({ timeout: 30_000 });
    await page.getByRole("list", { name: "Recents" }).locator(`a[data-session="${demo.session.id}"]`).click();
    await expect(page).toHaveURL(/\/session\/[^/]+$/, { timeout: 30_000 });
    const id = decodeURIComponent(new URL(page.url()).pathname.split("/").pop()!);
    expect(id).not.toBe(demo.session.id);

    const chat = page.getByRole("region", { name: "Chat" });
    await expect(chat.getByText(opening(demo)).first()).toBeVisible({ timeout: 30_000 });
    // A session like any other: the composer, no notice, nothing about a copy or a sample.
    await expect(chat.getByRole("textbox", { name: "Message" })).toBeEditable();
    await expect(chat.getByTestId("sample-notice")).toHaveCount(0);
    await expect(page.getByRole("button", { name: "Continue from this" })).toHaveCount(0);

    const canvas = page.getByRole("region", { name: "Canvas" });
    await expect(canvas.getByRole("heading", { name: demo.session.title, exact: true })).toBeVisible();
    const viewer = page.getByTestId("viewer");
    await expect(viewer).toHaveAttribute("data-state", "ready", { timeout: 30_000 });
    await expect(viewer).toHaveAttribute("data-fits", "true");
    await expect(canvas.getByTestId("model-parts")).toHaveText(demo.parts.join(", "));

    // Every located check result is a place of its constraint in the Constraints panel, with the fixture's status.
    await expect(canvas.getByTestId("check-pins")).toBeAttached({ timeout: 30_000 });
    const panel = await openConstraints(canvas);
    await expect(panel.getByTestId("checks-summary")).toContainText(" pass");
    const located = locatedResults(demo.checks);
    for (const r of located) {
      const item = constraintItem(canvas, r);
      if (r.status === "fail") await expect(item).toHaveAttribute("data-status", "fail");
      if ((await item.getAttribute("aria-expanded")) !== "true") await item.click();
      await expect(constraintPlaces(canvas).and(canvas.locator(`[data-check="${r.id}"]`))).toHaveAttribute("data-status", r.status);
    }

    // The row is now this copy, in the demo's place and still tagged Demo: no second row for it.
    const recents = page.getByRole("list", { name: "Recents" });
    const row = recents.locator(`a[data-session="${id}"]`);
    await expect(row).toHaveAttribute("href", `/session/${id}`);
    await expect(row.getByTestId("demo-tag")).toBeVisible();
    await expect(recents.locator(`a[data-session="${demo.session.id}"]`)).toHaveCount(0);
    await expect(recents.locator("a[data-session]").filter({ hasText: demo.session.title })).toHaveCount(1);
    expect(errors).toEqual([]);
  });
}

test("each demo's copy has its board and builds, and only those on an older board revision offer the next one", async ({ request }) => {
  const withNext: string[] = [];
  for (const demo of DEMOS) {
    const before = await (await request.get(`/api/sessions/${demo.session.id}`)).json();
    const copy = await openDemo(request, demo.session.id);
    expect(copy.id).not.toBe(demo.session.id);
    expect(copy.demo).toBe(demo.session.id);
    expect(copy.title).toBe(demo.session.title);
    expect(copy.latest_build_id).not.toBeNull();
    // Opening it again gives the same copy; the original is unchanged.
    expect((await openDemo(request, demo.session.id)).id).toBe(copy.id);
    expect(await (await request.get(`/api/sessions/${demo.session.id}`)).json()).toEqual(before);
    const builds = (await (await request.get(`/api/sessions/${copy.id}/builds`)).json()) as unknown[];
    expect(builds.length).toBeGreaterThan(0);
    if (copy.board === null) expect(copy.next_revision).toBeNull();
    if (copy.next_revision) withNext.push(demo.session.id);
  }
  // The demo script starts from a demo on the previous board revision.
  expect(withNext.length).toBeGreaterThan(0);
});

test("a demo link opens the viewer's own copy, and Reset demo starts it over", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", (err) => errors.push(err.message));
  const demo = DEMOS[0];
  await page.goto(`/demo/${demo.session.id}`);
  await expect(page).toHaveURL(/\/session\/[^/]+$/, { timeout: 30_000 });
  const first = decodeURIComponent(new URL(page.url()).pathname.split("/").pop()!);
  const chat = page.getByRole("region", { name: "Chat" });
  await expect(chat.getByText(opening(demo)).first()).toBeVisible({ timeout: 30_000 });

  // Reloading the demo link lands on the same copy.
  await page.goto(`/demo/${demo.session.id}`);
  await expect(page).toHaveURL(`/session/${first}`, { timeout: 30_000 });

  // Reset demo, in the row's menu: the copy is deleted and a fresh one opens.
  const recents = page.getByRole("list", { name: "Recents" });
  await recents.locator(`a[data-session="${first}"]`).hover();
  await recents.getByRole("button", { name: `Options for ${demo.session.title}` }).click();
  await recents.getByRole("menuitem", { name: "Reset demo" }).click();
  await recents.getByRole("menuitem", { name: "Reset", exact: true }).click();
  await expect(page).not.toHaveURL(`/session/${first}`, { timeout: 30_000 });
  await expect(page).toHaveURL(/\/session\/[^/]+$/, { timeout: 30_000 });
  await expect(chat.getByText(opening(demo)).first()).toBeVisible({ timeout: 30_000 });
  expect(errors).toEqual([]);
  await resetDemo(page.request, demo.session.id);
});
