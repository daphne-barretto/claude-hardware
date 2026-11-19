import { readFileSync } from "node:fs";
import path from "node:path";
import { expect, test, type APIRequestContext, type Locator, type Page } from "@playwright/test";

// Dimensions on the model and face drag against the mock session API, where setting
// a value locks the parameter and rebuilds (docs/architecture.md). Where each dimension sits and
// which faces drag come from fixtures/parameters.json.
test.skip(!!process.env.E2E_BASE_URL, "the mock API runs only with the local dev servers");

type End = { point: [number, number, number]; part?: string | null; face?: string | null };
type Parameter = {
  name: string;
  value: number | string;
  unit?: string | null;
  description?: string;
  locked: boolean;
  dimension?: { kind: string; start: End; end: End } | null;
  drag?: { faces: string[]; axis: [number, number, number] } | null;
};
type Report = {
  summary: { pass: number; fail: number; untested: number };
  results: { title: string; status: string }[];
};

const fixture = <T>(name: string) =>
  JSON.parse(readFileSync(path.join(__dirname, "../../fixtures", name), "utf8")) as T;
const PARAMS = fixture<Parameter[]>("parameters.json");
const BUILD = fixture<{ id: string }>("build.json");
const CHECKS = fixture<Report>("checks.json");
const SESSION = fixture<{ prompt: string }>("session.json");
const label = (p: Parameter) => p.description || p.name;
const DIMENSIONED = PARAMS.filter((p) => p.dimension);
const DRAGGABLE = PARAMS.filter((p) => p.drag);
const DRAG_FACES = new Set(DRAGGABLE.flatMap((p) => p.drag!.faces));

/** A mock session whose recorded run has finished, opened on its canvas with dimensions shown. */
async function openDimensions(page: Page, request: APIRequestContext) {
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
  const toggle = canvas.getByRole("button", { name: "Dimensions", exact: true });
  await toggle.click();
  await expect(toggle).toHaveAttribute("aria-pressed", "true");
  const dims = canvas.getByRole("group", { name: "Dimensions" });
  await expect(dims.getByRole("button", { name: /^Edit / })).toHaveCount(DIMENSIONED.length);
  return { id: session.id as string, viewer, canvas, dims };
}

/** A canvas point (CSS pixels, from a dimension's data-start or data-end) in page pixels. */
async function onPage(viewer: Locator, point: string): Promise<[number, number]> {
  const box = (await viewer.boundingBox())!;
  const [x, y] = point.split(",").map(Number);
  return [box.x + x, box.y + y];
}

/** The ends of a dimension as drawn: [start, end] in page pixels. */
async function drawnEnds(page: Page, viewer: Locator, name: string) {
  const g = page.locator(`[data-dimension="${name}"]`);
  return [
    await onPage(viewer, (await g.getAttribute("data-start"))!),
    await onPage(viewer, (await g.getAttribute("data-end"))!),
  ];
}

/** True if a page point is on the 3D view itself, not under a panel or a dimension label. */
async function onView(page: Page, point: [number, number]) {
  return page.evaluate(([x, y]) => !!document.elementFromPoint(x, y)?.closest('[data-testid="viewer"]'), point);
}

/** The face a click at a page point selects (and clears the selection again); null off the view. */
async function faceAt(page: Page, viewer: Locator, point: [number, number]) {
  if (!(await onView(page, point))) return null;
  const [x, y] = point;
  await page.mouse.click(x, y);
  const face = await viewer.getAttribute("data-selected");
  await page.keyboard.press("Escape");
  await expect(viewer).toHaveAttribute("data-selected", "");
  return face;
}

/** Points from `from` toward `to`: the end itself, then a little way in, for an end under a label. */
const nearEnd = (from: [number, number], to: [number, number]): [number, number][] =>
  [0, 0.03, 0.06, 0.1].map((t) => [from[0] + (to[0] - from[0]) * t, from[1] + (to[1] - from[1]) * t]);

/** A point on a face that drags a parameter, from where its dimension ends on that face. */
async function grabPoint(page: Page, viewer: Locator) {
  for (const param of DRAGGABLE) {
    const [start, end] = await drawnEnds(page, viewer, param.name);
    const onDragFace = param.dimension!.end.face && param.drag!.faces.includes(param.dimension!.end.face);
    const [grab, other] = onDragFace ? [end, start] : [start, end];
    for (const point of nearEnd(grab, other)) {
      const face = await faceAt(page, viewer, point);
      if (face && param.drag!.faces.includes(face)) return { param, grab: point, other };
    }
  }
  return null;
}

async function drag(page: Page, [x, y]: [number, number], [dx, dy]: [number, number], beforeUp?: () => Promise<void>) {
  await page.mouse.move(x, y);
  await page.mouse.down();
  for (let i = 1; i <= 8; i++) await page.mouse.move(x + (dx * i) / 8, y + (dy * i) / 8);
  await beforeUp?.();
  await page.mouse.up();
}

async function expectLockedAndRebuilt(page: Page, canvas: Locator, viewer: Locator, param: Parameter) {
  // The same lock, rebuild and failing checks as typing in the parameters panel.
  const panel = canvas.getByRole("region", { name: "Parameters" });
  await expect(panel.locator(`[data-parameter="${param.name}"]`)).toHaveAttribute("data-locked", "true");
  await expect(panel.getByRole("button", { name: `Unlock ${label(param)}` })).toBeVisible();
  const rebuild = panel.getByRole("region", { name: "Rebuild" });
  await expect(rebuild).toHaveAttribute("data-state", "done", { timeout: 30_000 });
  const buildId = await rebuild.getAttribute("data-build");
  expect(buildId).not.toBe(BUILD.id);
  await expect(viewer).toHaveAttribute("data-build", buildId!);
  const { summary } = CHECKS;
  await expect(rebuild.getByTestId("rebuild-summary")).toContainText(
    `${summary.pass} pass · ${summary.fail} fail · ${summary.untested} untested`,
  );
  await expect(rebuild.getByRole("list", { name: "Failing checks" }).getByRole("listitem")).toHaveCount(summary.fail);
  // And on the model, the dimension shows its lock.
  await expect(
    canvas.getByRole("group", { name: "Dimensions" }).locator(`[data-parameter="${param.name}"]`),
  ).toHaveAttribute("data-locked", "true");
}

test("the fixtures give dimensions and draggable faces to use", () => {
  expect(DIMENSIONED.length).toBeGreaterThan(0);
  expect(DRAGGABLE.length).toBeGreaterThan(0);
});

test("every dimension is drawn on the model with its value", async ({ page, request }) => {
  const { dims } = await openDimensions(page, request);
  for (const p of DIMENSIONED) {
    const button = dims.getByRole("button", { name: `Edit ${label(p)}`, exact: true });
    await expect(button).toBeVisible();
    await expect(button).toContainText(String(Number((p.value as number).toFixed(3))));
    await expect(page.locator(`[data-dimension="${p.name}"]`)).toHaveCount(1);
  }
  for (const p of PARAMS.filter((q) => !q.dimension))
    await expect(dims.getByRole("button", { name: `Edit ${label(p)}`, exact: true })).toHaveCount(0);
  // Hidden again with the toggle.
  await page.getByRole("button", { name: "Dimensions", exact: true }).click();
  await expect(dims).toHaveCount(0);
});

test("clicking a dimension opens a field: typing a value locks it, rebuilds and shows the failing checks", async ({
  page,
  request,
}) => {
  const errors: string[] = [];
  page.on("pageerror", (err) => errors.push(err.message));
  const { id, viewer, canvas, dims } = await openDimensions(page, request);
  const param = DIMENSIONED[0];
  const value = Number(((param.value as number) + 1).toFixed(3));

  await dims.getByRole("button", { name: `Edit ${label(param)}`, exact: true }).click();
  const field = dims.getByRole("textbox", { name: label(param), exact: true });
  await expect(field).toBeFocused();
  expect(Number(await field.inputValue())).toBeCloseTo(param.value as number);

  const patch = page.waitForRequest((r) => r.method() === "PATCH" && r.url().endsWith(`/parameters/${param.name}`));
  await field.fill(String(value));
  await field.press("Enter");
  expect((await patch).postDataJSON()).toEqual({ value });
  await expect(field).toHaveCount(0);
  await expectLockedAndRebuilt(page, canvas, viewer, param);

  const saved = (await (await request.get(`/api/sessions/${id}/parameters`)).json()) as Parameter[];
  expect(saved.find((p) => p.name === param.name)).toMatchObject({ value, locked: true });
  expect(errors).toEqual([]);
});

test("a dimension refuses a value that isn't a number, and Escape closes it without an edit", async ({
  page,
  request,
}) => {
  const { dims } = await openDimensions(page, request);
  const param = DIMENSIONED[0];
  let patched = false;
  page.on("request", (r) => {
    if (r.method() === "PATCH") patched = true;
  });
  await dims.getByRole("button", { name: `Edit ${label(param)}`, exact: true }).click();
  const field = dims.getByRole("textbox", { name: label(param), exact: true });
  await field.fill("wide");
  await field.press("Enter");
  await expect(field).toHaveAttribute("aria-invalid", "true");
  await expect(dims.getByText("Type a number.")).toBeVisible();
  await field.press("Escape");
  await expect(field).toHaveCount(0);
  await expect(dims.getByRole("button", { name: `Edit ${label(param)}`, exact: true })).toBeVisible();
  expect(patched).toBe(false);
});

test("dragging a face of a draggable feature changes its parameter as a hand edit", async ({ page, request }) => {
  const { id, viewer, canvas, dims } = await openDimensions(page, request);
  // A draggable parameter whose dragged end can be grabbed from this view.
  const found = await grabPoint(page, viewer);
  expect(found, "no draggable face can be grabbed from the home view").not.toBeNull();
  const { param, grab, other } = found!;

  // Outward: away from the dimension's other end, which makes the value bigger.
  const len = Math.hypot(grab[0] - other[0], grab[1] - other[1]);
  const away: [number, number] = [((grab[0] - other[0]) / len) * 40, ((grab[1] - other[1]) / len) * 40];
  const patch = page.waitForRequest((r) => r.method() === "PATCH" && r.url().endsWith(`/parameters/${param.name}`));
  const azimuth = await viewer.getAttribute("data-azimuth");
  await drag(page, grab, away, async () => {
    // While dragging, the dimension follows and shows the value it will set.
    const live = dims.locator(`[data-parameter="${param.name}"]`);
    await expect(live).toHaveAttribute("data-dragging", "true");
    const shown = Number((await live.getByRole("button").innerText()).replace(/[^\d.]/g, ""));
    expect(shown).toBeGreaterThan(param.value as number);
  });
  const body = (await patch).postDataJSON() as { value: number };
  expect(Object.keys(body)).toEqual(["value"]);
  expect(body.value).toBeGreaterThan(param.value as number);
  // The face moved; the view didn't orbit.
  await expect(viewer).toHaveAttribute("data-azimuth", azimuth!);
  await expectLockedAndRebuilt(page, canvas, viewer, param);

  const saved = (await (await request.get(`/api/sessions/${id}/parameters`)).json()) as Parameter[];
  expect(saved.find((p) => p.name === param.name)).toMatchObject({ value: body.value, locked: true });
});

test("faces of other features don't drag: the view orbits and nothing is edited", async ({ page, request }) => {
  const { viewer } = await openDimensions(page, request);
  let patched = false;
  page.on("request", (r) => {
    if (r.method() === "PATCH") patched = true;
  });
  // A point on the model on a face no parameter drags: across the model's box on screen, turning
  // the view 90° at a time until one is in view (the panels can cover part of the model).
  const box = (await viewer.boundingBox())!;
  let grab: [number, number] | null = null;
  for (let turn = 0; turn < 4 && !grab; turn++) {
    if (turn) {
      await viewer.focus();
      await page.keyboard.press("ArrowLeft");
      await expect(viewer).toHaveAttribute("data-animating", "false");
    }
    const [left, top, width, height] = (await viewer.getAttribute("data-model-rect"))!.split(",").map(Number);
    for (let j = 1; j < 6 && !grab; j++)
      for (let i = 1; i < 8 && !grab; i++) {
        const point: [number, number] = [box.x + left + (width * i) / 8, box.y + top + (height * j) / 6];
        const face = await faceAt(page, viewer, point);
        if (face && !DRAG_FACES.has(face)) grab = point;
      }
  }
  expect(grab, "no face that doesn't drag is in view").not.toBeNull();
  const azimuth = await viewer.getAttribute("data-azimuth");
  await drag(page, grab!, [60, 0]);
  await expect(viewer).not.toHaveAttribute("data-azimuth", azimuth!);
  expect(patched).toBe(false);
});

test("with dimensions off, faces don't drag", async ({ page, request }) => {
  const { viewer } = await openDimensions(page, request);
  const found = await grabPoint(page, viewer);
  expect(found, "no draggable face can be grabbed from the home view").not.toBeNull();
  await page.getByRole("button", { name: "Dimensions", exact: true }).click();
  let patched = false;
  page.on("request", (r) => {
    if (r.method() === "PATCH") patched = true;
  });
  const azimuth = await viewer.getAttribute("data-azimuth");
  await drag(page, found!.grab, [60, 0]);
  await expect(viewer).not.toHaveAttribute("data-azimuth", azimuth!);
  expect(patched).toBe(false);
});
