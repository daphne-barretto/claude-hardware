import { readFileSync } from "node:fs";
import path from "node:path";
import { expect, test, type APIRequestContext, type Page } from "@playwright/test";
import { sketchTargets } from "./sketchFaces";

// Sketch on a face against the mock session API, where a sketch is a locked feature in
// the code and rebuilds like a hand edit (docs/architecture.md). The faces come from the fixture
// mesh (e2e/sketchFaces.ts) and the checks from fixtures/checks.json.
test.skip(!!process.env.E2E_BASE_URL, "the mock API runs only with the local dev servers");

type Report = { summary: { pass: number; fail: number; untested: number } };
const fixture = <T,>(name: string) => JSON.parse(readFileSync(path.join(__dirname, "../../fixtures", name), "utf8")) as T;
const BUILD = fixture<{ id: string }>("build.json");
const CHECKS = fixture<Report>("checks.json");
const SESSION = fixture<{ prompt: string }>("session.json");
const { flat, curved } = sketchTargets();

/** A mock session whose recorded run has finished, on its canvas, with `faceId` picked and the sketch panel open. */
async function openSketch(page: Page, request: APIRequestContext, faceId = flat.id) {
  const session = await (await request.post("/api/sessions", { data: {} })).json();
  await request.post(`/api/sessions/${session.id}/messages`, { data: { text: SESSION.prompt } });
  await expect
    .poll(async () => (await (await request.get(`/api/sessions/${session.id}`)).json()).status, { timeout: 30_000 })
    .not.toBe("running");
  await page.goto(`/session/${session.id}`);
  await expect(page.locator("[data-shell-ready=true]")).toBeAttached({ timeout: 30_000 });
  const viewer = page.getByTestId("viewer");
  await expect(viewer).toHaveAttribute("data-state", "ready", { timeout: 30_000 });
  const canvas = page.getByRole("region", { name: "Canvas" });
  const parts = canvas.getByRole("region", { name: "Parts" });
  await parts.getByRole("textbox", { name: "Select by ID" }).fill(faceId);
  await parts.getByRole("textbox", { name: "Select by ID" }).press("Enter");
  await expect(viewer).toHaveAttribute("data-selected", faceId);
  await canvas.getByRole("button", { name: "Sketch" }).click();
  const panel = canvas.getByRole("region", { name: "Sketch" });
  await expect(panel.getByTestId("sketch-face")).toHaveAttribute("data-planar", faceId === flat.id ? "true" : "false");
  return { id: session.id as string, viewer, canvas, panel };
}

async function fill(panel: ReturnType<Page["getByRole"]>, values: Record<string, string>) {
  for (const [label, value] of Object.entries(values)) await panel.getByRole("textbox", { name: label, exact: true }).fill(value);
}

const CASES = [
  { shape: "Circle", operation: "Cut", fields: { Diameter: "3", "Position x": "0.5", "Position y": "-0.5", Depth: "0.8" } },
  {
    shape: "Slot",
    operation: "Extrude",
    fields: { Length: "4", "Slot width": "1.5", "Position x": "-1", "Position y": "0", "Height out": "1" },
  },
  {
    shape: "Rectangle",
    operation: "Cut",
    fields: { Width: "3", Height: "2", "Position x": "0", "Position y": "1", Depth: "0.5" },
  },
  {
    shape: "Vent",
    operation: "Extrude",
    fields: { Width: "5", Height: "2", Slots: "3", "Position x": "0", "Position y": "0", "Height out": "0.6" },
  },
] as const;

const FIELD_NAMES: Record<string, string> = {
  Diameter: "width",
  Length: "width",
  Width: "width",
  "Slot width": "height",
  Height: "height",
  Slots: "count",
  "Position x": "x",
  "Position y": "y",
  Depth: "depth",
  "Height out": "depth",
};

test("each shape can be placed on a flat face with typed size and position, then cut or extruded", async ({
  page,
  request,
}) => {
  const errors: string[] = [];
  page.on("pageerror", (err) => errors.push(err.message));
  const { id, viewer, panel } = await openSketch(page, request);
  await expect(panel.getByTestId("sketch-face")).toContainText(flat.id);

  for (const [n, c] of CASES.entries()) {
    await panel.getByRole("radiogroup", { name: "Shape" }).getByRole("radio", { name: c.shape }).click();
    await panel.getByRole("radiogroup", { name: "Operation" }).getByRole("radio", { name: c.operation }).click();
    await fill(panel, c.fields);
    const post = page.waitForRequest((r) => r.method() === "POST" && r.url().endsWith(`/sessions/${id}/sketches`));
    const response = page.waitForResponse((r) => r.request().method() === "POST" && r.url().endsWith("/sketches"));
    await panel.getByRole("button", { name: `${c.operation} ${c.shape.toLowerCase()}` }).click();

    const body = (await post).postDataJSON();
    const expected: Record<string, unknown> = {
      face_id: flat.id,
      shape: c.shape.toLowerCase(),
      operation: c.operation.toLowerCase(),
      height: null,
      count: null,
    };
    for (const [label, value] of Object.entries(c.fields)) expected[FIELD_NAMES[label]] = Number(value);
    expect(body).toMatchObject(expected);
    expect((await response).status()).toBe(201);

    const tag = `sketch_${n + 1}`;
    await expect(panel.getByTestId("sketch-made")).toHaveAttribute("data-tag", tag);
    // Rebuilt: the canvas follows the new build.
    const rebuild = panel.getByRole("region", { name: "Rebuild" });
    await expect(rebuild).toHaveAttribute("data-state", "done", { timeout: 30_000 });
    const buildId = await rebuild.getAttribute("data-build");
    expect(buildId).not.toBe(BUILD.id);
    await expect(viewer).toHaveAttribute("data-build", buildId!);
  }

  // The API has all four, locked, on the face.
  const sketches = await (await request.get(`/api/sessions/${id}/sketches`)).json();
  expect(sketches.map((s: { tag: string; shape: string; operation: string; locked: boolean; face_id: string }) => s)).toEqual(
    CASES.map((c, n) =>
      expect.objectContaining({
        tag: `sketch_${n + 1}`,
        shape: c.shape.toLowerCase(),
        operation: c.operation.toLowerCase(),
        locked: true,
        face_id: flat.id,
        part: flat.part,
      }),
    ),
  );
  expect(errors).toEqual([]);
});

test("sizes typed in inches are sent in millimetres", async ({ page, request }) => {
  const { canvas, panel } = await openSketch(page, request);
  await canvas.getByRole("radiogroup", { name: "Units" }).getByRole("radio", { name: "in" }).click();
  await fill(panel, { Diameter: "0.1", "Position x": "0", "Position y": "0", Depth: "0.02" });
  const post = page.waitForRequest((r) => r.method() === "POST" && r.url().endsWith("/sketches"));
  await panel.getByRole("button", { name: "Cut circle" }).click();
  const body = (await post).postDataJSON();
  expect(body.width).toBeCloseTo(2.54);
  expect(body.depth).toBeCloseTo(0.508);
});

test("the new feature appears in the feature tree and the code, locked, and the checks rerun", async ({ page, request }) => {
  const { viewer, canvas, panel } = await openSketch(page, request);
  await panel.getByRole("radiogroup", { name: "Shape" }).getByRole("radio", { name: "Vent" }).click();
  await panel.getByRole("button", { name: "Cut vent" }).click();

  // The checks rerun on the rebuild, which the canvas shows.
  const rebuild = panel.getByRole("region", { name: "Rebuild" });
  await expect(rebuild).toHaveAttribute("data-state", "done", { timeout: 30_000 });
  const buildId = (await rebuild.getAttribute("data-build"))!;
  await expect(viewer).toHaveAttribute("data-build", buildId);
  const { summary } = CHECKS;
  await expect(rebuild.getByTestId("rebuild-summary")).toContainText(
    `${summary.pass} pass · ${summary.fail} fail · ${summary.untested} untested`,
  );

  // In the feature tree, under its part, locked.
  const features = canvas.getByRole("region", { name: "Parts" }).getByRole("list", { name: `Features of ${flat.part}` });
  const row = features.locator('[data-feature="sketch_1"]');
  await expect(row).toHaveAttribute("data-locked", "true");
  await expect(row.getByRole("img", { name: "sketch_1 is locked" })).toBeVisible();
  // Only the sketched feature is locked.
  await expect(canvas.locator('[data-feature][data-locked="true"]')).toHaveCount(1);

  // In the code: its line link opens the code that applies it.
  await row.getByRole("button", { name: "Lines of sketch_1" }).click();
  const code = canvas.getByRole("region", { name: "Code" }).getByTestId("code-source");
  await expect(code).toHaveAttribute("data-build", buildId);
  const lines = (await code.getAttribute("data-lines"))!.split("-").map(Number);
  const marked = code.locator("[data-marked]");
  await expect(marked).toHaveCount(lines[1] - lines[0] + 1);
  await expect(marked.first()).toContainText('"sketch_1"');
  await expect(code).toContainText("Sketched features");
});

test("sketching needs one flat face, and says why it can't", async ({ page, request }) => {
  const { canvas, panel } = await openSketch(page, request, curved.id);
  const submit = panel.getByRole("button", { name: "Cut circle" });
  await expect(submit).toBeDisabled();
  await expect(submit).toHaveAttribute("title", "Pick a flat face: this one is curved");
  await expect(panel.getByTestId("sketch-face")).toContainText("curved");

  // Nothing picked.
  await page.keyboard.press("Escape");
  await canvas.getByTestId("viewer").focus();
  await page.keyboard.press("Escape");
  await expect(canvas.getByTestId("viewer")).toHaveAttribute("data-selected", "");
  await expect(submit).toHaveAttribute("title", "Pick a flat face on the model");

  // A size that doesn't fit is refused by the API, and says so.
  const parts = canvas.getByRole("region", { name: "Parts" });
  await parts.getByRole("textbox", { name: "Select by ID" }).fill(flat.id);
  await parts.getByRole("textbox", { name: "Select by ID" }).press("Enter");
  await expect(submit).toBeEnabled();
  await fill(panel, { Diameter: "1000" });
  await submit.click();
  await expect(panel.getByRole("alert")).toContainText("runs off");
  // A typed value that isn't a number is refused without a request.
  let posted = false;
  page.on("request", (r) => {
    if (r.method() === "POST" && r.url().endsWith("/sketches")) posted = true;
  });
  await fill(panel, { Diameter: "abc" });
  await submit.click();
  await expect(panel.getByText("Type a number.")).toBeVisible();
  expect(posted).toBe(false);
});
