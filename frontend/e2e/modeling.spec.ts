import { readFileSync } from "node:fs";
import path from "node:path";
import { expect, test, type APIRequestContext, type Locator, type Page } from "@playwright/test";
import { sketchTargets } from "./sketchFaces";

// Modeling tools against the mock session API, where every tool's edit is a locked
// feature in the code and rebuilds like a hand edit (docs/architecture.md). The faces, edges, parts
// and features come from the fixture mesh, so none is named here; the checks from
// fixtures/checks.json.
test.skip(!!process.env.E2E_BASE_URL, "the mock API runs only with the local dev servers");

type Gltf = { nodes: { extras?: { part?: string; faces?: { id: string; feature: string }[]; edges?: { id: string }[] } }[] };
const fixture = <T,>(name: string) => JSON.parse(readFileSync(path.join(__dirname, "../../fixtures", name), "utf8")) as T;
const BUILD = fixture<{ id: string }>("build.json");
const SESSION = fixture<{ prompt: string }>("session.json");
const MESH = fixture<Gltf>("enclosure.gltf");
const { flat } = sketchTargets();
const parts = MESH.nodes.flatMap((n) => (n.extras?.part ? [n.extras] : []));
const EDGE = parts.find((p) => p.part === flat.part)!.edges![0].id;
// A feature the code made (not a part body), with faces on the model: mirrored and patterned.
const SOURCE = fixture<{ features: { tag: string; part: string }[] }>("build-source.json");
const onModel = new Set(parts.flatMap((p) => p.faces!.map((f) => f.feature)));
const FEATURE = SOURCE.features.find((f) => f.tag !== f.part && onModel.has(f.tag))!.tag;

type Question = { question_id: string; options: { id: string }[] };
const QUESTION = readFileSync(path.join(__dirname, "../../fixtures/session-events.jsonl"), "utf8")
  .split("\n")
  .filter(Boolean)
  .map((line) => JSON.parse(line).event)
  .find((e) => e.type === "question") as Question;

/** A mock session whose recorded run has finished, on its canvas; with `answer`, its question answered too. */
async function openCanvas(page: Page, request: APIRequestContext, answer = false) {
  const session = await (await request.post("/api/sessions", { data: {} })).json();
  const status = async () => (await (await request.get(`/api/sessions/${session.id}`)).json()).status;
  await request.post(`/api/sessions/${session.id}/messages`, { data: { text: SESSION.prompt } });
  await expect.poll(status, { timeout: 30_000 }).not.toBe("running");
  if (answer) {
    // Fix requests post text, which the session takes only once no question waits.
    await request.post(`/api/sessions/${session.id}/messages`, {
      data: { text: "", answer: { question_id: QUESTION.question_id, option_id: QUESTION.options[0].id } },
    });
    await expect.poll(status, { timeout: 30_000 }).toBe("idle");
  }
  await page.goto(`/session/${session.id}`);
  await expect(page.locator("[data-shell-ready=true]")).toBeAttached({ timeout: 30_000 });
  const viewer = page.getByTestId("viewer");
  await expect(viewer).toHaveAttribute("data-state", "ready", { timeout: 30_000 });
  const canvas = page.getByRole("region", { name: "Canvas" });
  return { id: session.id as string, viewer, canvas, toolbar: canvas.getByRole("toolbar", { name: "Modeling tools" }) };
}

async function pick(canvas: Locator, id: string) {
  const box = canvas.getByRole("region", { name: "Parts" }).getByRole("textbox", { name: "Select by ID" });
  await box.fill(id);
  await box.press("Enter");
}

async function fill(panel: Locator, values: Record<string, string>) {
  for (const [label, value] of Object.entries(values)) await panel.getByRole("textbox", { name: label, exact: true }).fill(value);
}

const shot = (page: Page, name: string) => page.screenshot({ path: test.info().outputPath(`${name}.png`) });

type Case = {
  tool: string;
  kind: string;
  target: string;
  fields: Record<string, string>;
  setup?: (panel: Locator) => Promise<void>;
  body: Record<string, unknown>;
};

const CASES: Case[] = [
  {
    tool: "Move",
    kind: "move",
    target: flat.part,
    fields: { "Move x": "2", "Move y": "0", "Move z": "0.5", "Rotate about z": "15" },
    body: { part: flat.part, dx: 2, dy: 0, dz: 0.5, rotation: 15 },
  },
  {
    tool: "Push/pull",
    kind: "push_pull",
    target: flat.id,
    fields: { "Distance (− pushes in)": "-0.4" },
    body: { face_id: flat.id, distance: -0.4 },
  },
  {
    tool: "Hole",
    kind: "hole",
    target: flat.id,
    fields: { "Position x": "1", "Position y": "0" },
    setup: (panel) => panel.getByRole("checkbox", { name: "Heat-set insert" }).check(),
    body: { face_id: flat.id, insert: true, x: 1, y: 0 },
  },
  {
    tool: "Boss",
    kind: "boss",
    target: flat.id,
    fields: { Diameter: "3", Height: "2", "Position x": "-1", "Position y": "0" },
    body: { face_id: flat.id, diameter: 3, height: 2, x: -1, y: 0, insert: false },
  },
  {
    tool: "Rib",
    kind: "rib",
    target: flat.id,
    fields: { Length: "4", Thickness: "0.8", Height: "1.5", "Position x": "0", "Position y": "1", Angle: "30" },
    body: { face_id: flat.id, length: 4, thickness: 0.8, height: 1.5, x: 0, y: 1, rotation: 30 },
  },
  { tool: "Fillet", kind: "fillet", target: EDGE, fields: { Radius: "0.5" }, body: { edge_id: EDGE, radius: 0.5 } },
  { tool: "Chamfer", kind: "chamfer", target: EDGE, fields: { Size: "0.3" }, body: { edge_id: EDGE, distance: 0.3 } },
  { tool: "Shell", kind: "shell", target: flat.part, fields: { "Wall thickness": "1.2" }, body: { part: flat.part, thickness: 1.2 } },
  {
    tool: "Mirror",
    kind: "mirror",
    target: FEATURE,
    fields: {},
    setup: (panel) => panel.getByRole("radiogroup", { name: "Mirror across" }).getByRole("radio", { name: "y" }).click(),
    body: { source: FEATURE, axis: "y" },
  },
  {
    tool: "Pattern",
    kind: "pattern",
    target: FEATURE,
    fields: { "Copies in all": "4", Spans: "360" },
    setup: (panel) => panel.getByRole("radiogroup", { name: "Pattern" }).getByRole("radio", { name: "Circular" }).click(),
    body: { source: FEATURE, pattern: "circular", count: 4, angle: 360 },
  },
];

test("each tool changes the code, rebuilds and rechecks, and its edit is locked", async ({ page, request }) => {
  test.setTimeout(180_000);
  const errors: string[] = [];
  page.on("pageerror", (err) => errors.push(err.message));
  const { id, viewer, canvas, toolbar } = await openCanvas(page, request);
  await expect(toolbar).toBeVisible();
  await shot(page, "toolbar");

  for (const c of CASES) {
    await toolbar.getByRole("button", { name: c.tool, exact: true }).click();
    const panel = canvas.getByRole("region", { name: "Modeling" });
    await expect(panel).toHaveAttribute("data-tool", c.kind);
    await pick(canvas, c.target);
    await expect(panel.getByTestId("edit-target")).toHaveAttribute("data-ready", "true");
    await c.setup?.(panel);
    await fill(panel, c.fields);
    const post = page.waitForRequest((r) => r.method() === "POST" && r.url().endsWith(`/sessions/${id}/edits`));
    const response = page.waitForResponse((r) => r.request().method() === "POST" && r.url().endsWith("/edits"));
    await panel.getByRole("button", { name: `Add ${c.tool.toLowerCase()}` }).click();
    expect((await post).postDataJSON()).toMatchObject({ kind: c.kind, ...c.body });
    const res = await response;
    expect(res.status()).toBe(201);
    const tag = (await res.json()).tag as string;
    expect(tag).toBe(`${c.kind}_hand_1`);

    // Rebuilt and rechecked: the canvas follows the new build.
    const rebuild = panel.getByRole("region", { name: "Rebuild" });
    await expect(rebuild).toHaveAttribute("data-state", "done", { timeout: 30_000 });
    const buildId = (await rebuild.getAttribute("data-build"))!;
    expect(buildId).not.toBe(BUILD.id);
    await expect(viewer).toHaveAttribute("data-build", buildId);
    await expect(panel.getByTestId("edit-notice")).toContainText(`${tag} is locked`);
    await expect(panel.getByRole("region", { name: "Hand edits" }).locator(`[data-edit="${tag}"]`)).toBeVisible();
    await shot(page, `tool-${c.kind}`);

    // In the code, at its line, locked.
    const source = await (await request.get(`/api/sessions/${id}/builds/${buildId}/source`)).json();
    const feature = source.features.find((f: { tag: string }) => f.tag === tag);
    expect(feature).toMatchObject({ locked: true });
    expect(source.code.split("\n")[feature.lines[0] - 1]).toContain(`"${tag}"`);
  }

  const edits = await (await request.get(`/api/sessions/${id}/edits`)).json();
  expect(edits.map((e: { tag: string; locked: boolean }) => [e.tag, e.locked])).toEqual(CASES.map((c) => [`${c.kind}_hand_1`, true]));
  expect(errors).toEqual([]);
});

test("a hand edit can be changed, suppressed and deleted, with what copied it", async ({ page, request }) => {
  const { id, canvas, toolbar } = await openCanvas(page, request);
  await toolbar.getByRole("button", { name: "Hole", exact: true }).click();
  const panel = canvas.getByRole("region", { name: "Modeling" });
  await pick(canvas, flat.id);
  await fill(panel, { Diameter: "2", Depth: "1" });
  await panel.getByRole("button", { name: "Add hole" }).click();
  const list = panel.getByRole("region", { name: "Hand edits" });
  await expect(list.locator('[data-edit="hole_hand_1"]')).toBeVisible();
  await request.post(`/api/sessions/${id}/edits`, {
    data: { build_id: (await (await request.get(`/api/sessions/${id}`)).json()).latest_build_id, kind: "mirror", source: "hole_hand_1", axis: "x" },
  });

  // Change its values: still locked.
  await list.getByRole("button", { name: "Edit hole_hand_1" }).click();
  await fill(panel, { Diameter: "1.5" });
  const patch = page.waitForRequest((r) => r.method() === "PATCH" && r.url().endsWith("/edits/hole_hand_1"));
  await panel.getByRole("button", { name: "Save hole_hand_1" }).click();
  expect((await patch).postDataJSON()).toMatchObject({ diameter: 1.5 });
  await expect(panel.getByTestId("edit-notice")).toContainText("hole_hand_1 changed. Still locked");

  // Suppress it: kept in the design, out of the build.
  await list.getByRole("button", { name: "Suppress hole_hand_1" }).click();
  await expect(list.locator('[data-edit="hole_hand_1"]')).toHaveAttribute("data-suppressed", "true");
  await shot(page, "suppressed");

  // Delete it: the mirror that copied it goes too, and the rebuild shows it.
  await list.getByRole("button", { name: "Delete hole_hand_1" }).click();
  await expect(panel.getByTestId("edit-notice")).toContainText("Deleted hole_hand_1, and mirror_hand_1, which copied it");
  await expect(list.locator("[data-edit]")).toHaveCount(0);
  await expect(panel.getByRole("region", { name: "Rebuild" })).toHaveAttribute("data-state", "done", { timeout: 30_000 });
  await shot(page, "deleted");
});

test("undo and redo move the design along the timeline of changes", async ({ page, request }) => {
  const { id, viewer, canvas, toolbar } = await openCanvas(page, request);
  const undo = toolbar.getByRole("button", { name: "Undo" });
  const redo = toolbar.getByRole("button", { name: "Redo" });
  await expect(redo).toBeDisabled();

  await toolbar.getByRole("button", { name: "Rib", exact: true }).click();
  const panel = canvas.getByRole("region", { name: "Modeling" });
  await pick(canvas, flat.id);
  await panel.getByRole("button", { name: "Add rib" }).click();
  await expect(panel.getByRole("region", { name: "Rebuild" })).toHaveAttribute("data-state", "done", { timeout: 30_000 });
  const after = (await viewer.getAttribute("data-build"))!;

  // Undo: the design goes back to before the edit, rebuilt; the toast says what was undone.
  await expect(undo).toBeEnabled();
  const undone = page.waitForRequest((r) => r.method() === "POST" && r.url().endsWith("/history/undo"));
  await undo.click();
  await undone;
  await expect(canvas.getByRole("status", { name: "Design history" })).toContainText(/^Undid: You: rib /, { timeout: 30_000 });
  await expect(viewer).not.toHaveAttribute("data-build", after, { timeout: 30_000 });
  await expect.poll(async () => (await (await request.get(`/api/sessions/${id}/edits`)).json()).length).toBe(0);
  await expect(panel.getByRole("region", { name: "Hand edits" }).locator("[data-edit]")).toHaveCount(0);
  await shot(page, "undo");

  // Redo, with the keyboard: the edit comes back.
  await expect(redo).toBeEnabled();
  const again = page.waitForRequest((r) => r.method() === "POST" && r.url().endsWith("/history/redo"));
  await viewer.focus();
  await page.keyboard.press("ControlOrMeta+Shift+KeyZ");
  await again;
  await expect(canvas.getByRole("status", { name: "Design history" })).toContainText(/^Redid: You: rib /, { timeout: 30_000 });
  await expect(panel.getByRole("region", { name: "Hand edits" }).locator('[data-edit="rib_hand_1"]')).toBeVisible();
  await expect(redo).toBeDisabled();
  await shot(page, "redo");
});

test("an edit that breaks a check shows it on the model, and Claude is asked for a fix that keeps the edit", async ({
  page,
  request,
}) => {
  const { id, canvas, toolbar } = await openCanvas(page, request, true);
  await toolbar.getByRole("button", { name: "Push/pull", exact: true }).click();
  const panel = canvas.getByRole("region", { name: "Modeling" });
  await pick(canvas, flat.id);
  await fill(panel, { "Distance (− pushes in)": "-1" });
  await panel.getByRole("button", { name: "Add push/pull" }).click();
  const rebuild = panel.getByRole("region", { name: "Rebuild" });
  await expect(rebuild).toHaveAttribute("data-state", "done", { timeout: 30_000 });
  // The failing checks are listed with the rebuild and pinned on the model.
  await expect(rebuild.getByRole("list", { name: "Failing checks" })).toBeVisible();
  // (A pin under the open panel stays there, hidden.)
  await expect(canvas.locator('[data-testid="check-pin"][data-status="fail"]').first()).toBeAttached();

  const message = page.waitForRequest((r) => r.method() === "POST" && r.url().endsWith(`/sessions/${id}/messages`));
  await panel.getByTestId("ask-fix").click();
  const text = (await message).postDataJSON().text as string;
  expect(text).toContain("(push_pull_hand_1) breaks");
  expect(text).toContain("Keep my edit as it is");
  await expect(panel.getByTestId("ask-fix")).toHaveText("Asked Claude for a fix");
  // The edit stays: asking for a fix undoes nothing.
  const edits = await (await request.get(`/api/sessions/${id}/edits`)).json();
  expect(edits.map((e: { tag: string }) => e.tag)).toEqual(["push_pull_hand_1"]);
  await page.waitForTimeout(500);
  await shot(page, "ask-fix");
});

test("a hand edit is in the feature tree, locked, with a link to its line", async ({ page, request }) => {
  const { canvas, toolbar } = await openCanvas(page, request);
  await toolbar.getByRole("button", { name: "Boss", exact: true }).click();
  const panel = canvas.getByRole("region", { name: "Modeling" });
  await pick(canvas, flat.id);
  await panel.getByRole("checkbox", { name: "Heat-set insert" }).check();
  await panel.getByRole("button", { name: "Add boss" }).click();
  await expect(panel.getByRole("region", { name: "Rebuild" })).toHaveAttribute("data-state", "done", { timeout: 30_000 });
  const tree = canvas.getByRole("region", { name: "Parts" });
  for (const tag of ["boss_hand_1", "insert_hand_1"]) await expect(tree.locator(`[data-feature="${tag}"]`)).toHaveAttribute("data-locked", "true");
  await tree.getByRole("button", { name: "Lines of boss_hand_1" }).click();
  const code = canvas.getByRole("region", { name: "Code" }).getByTestId("code-source");
  await expect(code.locator("[data-marked]").first()).toContainText('"boss_hand_1"');
  await expect(code).toContainText("Modeled features");
  await shot(page, "feature-tree");
});
