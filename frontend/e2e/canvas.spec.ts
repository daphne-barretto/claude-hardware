import { readFileSync } from "node:fs";
import path from "node:path";
import { expect, test, type APIRequestContext, type Locator, type Page } from "@playwright/test";
import { openExample } from "./fixtures/sessions";

// The 3D canvas against the mock session API, which serves the contract
// fixtures (docs/architecture.md). Scenario values come from the fixture files, never from here.
test.skip(!!process.env.E2E_BASE_URL, "the mock API runs only with the local dev servers");

const fixture = (name: string) => JSON.parse(readFileSync(path.join(__dirname, "../../fixtures", name), "utf8"));
const BUILD = fixture("build.json") as { id: string; board: { name: string; revision: string } };
const MESH = fixture("enclosure.gltf") as { nodes: { extras?: { part?: string } }[] };
const SESSION = fixture("session.json") as { title: string; prompt: string };
const PARTS = MESH.nodes.flatMap((n) => (n.extras?.part ? [n.extras.part] : []));
const REVISION = `v${BUILD.board.revision}`;
const CHECKS = fixture("checks.json") as {
  summary: { pass: number; fail: number; untested: number; not_applicable?: number };
  results: { id: string; check: string; standard?: string | null; status: string }[];
};

/** A mock session whose recorded run has finished, so it has the fixture build. */
async function sessionWithBuild(request: APIRequestContext): Promise<string> {
  const session = await openExample(request);
  await request.post(`/api/sessions/${session.id}/messages`, { data: { text: SESSION.prompt } });
  await expect
    .poll(async () => (await (await request.get(`/api/sessions/${session.id}/builds`)).json()).length, {
      timeout: 30_000,
    })
    .toBeGreaterThan(0);
  return session.id;
}

async function openCanvas(page: Page, request: APIRequestContext) {
  const id = await sessionWithBuild(request);
  await page.goto(`/session/${id}`);
  await expect(page.locator("[data-shell-ready=true]")).toBeAttached({ timeout: 30_000 });
  const viewer = page.getByTestId("viewer");
  await expect(viewer).toHaveAttribute("data-state", "ready", { timeout: 30_000 });
  return { id, viewer, canvas: page.getByRole("region", { name: "Canvas" }) };
}

const attr = async (viewer: Locator, name: string) => (await viewer.getAttribute(name)) ?? "";

test("loads the fixture glTF through the mock API and fits it", async ({ page, request }) => {
  const errors: string[] = [];
  page.on("pageerror", (err) => errors.push(err.message));
  const { canvas, viewer, id } = await openCanvas(page, request);
  // The session opened from the example keeps the example's name (session naming).
  const { title } = (await (await request.get(`/api/sessions/${id}`)).json()) as { title: string };

  await expect(page.getByTestId("hardware-canvas")).toHaveAttribute("data-empty", "false");
  await expect(canvas.locator("canvas")).toBeVisible();
  await expect(viewer).toHaveAttribute("data-build", BUILD.id);
  await expect(viewer).toHaveAttribute("data-fits", "true");
  await expect(viewer).toHaveAttribute("data-zoom", "100");
  await expect(viewer).toHaveAttribute("data-view", "iso");
  await expect(canvas.getByTestId("model-parts")).toHaveText(PARTS.join(", "));
  await expect(canvas.getByRole("heading", { name: title })).toBeVisible();
  expect(errors).toEqual([]);
});

test("the canvas matches the reference screenshot", async ({ page, request }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  const { canvas } = await openCanvas(page, request);
  // The check pins are part of the canvas once the results arrive.
  await expect(canvas.getByTestId("check-pin").first()).toBeAttached();
  await expect(canvas).toHaveScreenshot("canvas.png", { maxDiffPixelRatio: 0.02 });
});

test("a build that arrives while the canvas is open is loaded", async ({ page, request }) => {
  const session = await openExample(request);
  await page.goto(`/session/${session.id}`);
  await expect(page.locator("[data-shell-ready=true]")).toBeAttached({ timeout: 30_000 });
  await expect(page.getByTestId("hardware-canvas")).toHaveAttribute("data-empty", "true");
  await request.post(`/api/sessions/${session.id}/messages`, { data: { text: SESSION.prompt } });
  await expect(page.getByTestId("viewer")).toHaveAttribute("data-state", "ready", { timeout: 30_000 });
  await expect(page.getByTestId("viewer")).toHaveAttribute("data-fits", "true");
});

test("orbit, pan and wheel zoom move the camera; fit brings the model back", async ({ page, request }) => {
  const { viewer, canvas } = await openCanvas(page, request);
  const box = (await viewer.boundingBox())!;
  // The middle of the model: drags that start on a check pin orbit and pan too.
  const cx = box.x + box.width / 2;
  const cy = box.y + box.height / 2;

  // Orbit: left drag.
  await page.mouse.move(cx, cy);
  await page.mouse.down();
  await page.mouse.move(cx + 120, cy + 40, { steps: 8 });
  await page.mouse.up();
  await expect(viewer).toHaveAttribute("data-view", "custom");

  // Pan: right drag moves the orbit centre.
  const target = await attr(viewer, "data-target");
  await page.mouse.move(cx, cy);
  await page.mouse.down({ button: "right" });
  await page.mouse.move(cx + 150, cy + 60, { steps: 8 });
  await page.mouse.up({ button: "right" });
  await expect.poll(() => attr(viewer, "data-target")).not.toBe(target);

  // Zoom: the wheel moves the camera in and the zoom readout follows.
  await page.mouse.move(cx, cy);
  for (let i = 0; i < 5; i++) await page.mouse.wheel(0, -200);
  await expect.poll(async () => Number(await attr(viewer, "data-zoom"))).toBeGreaterThan(100);
  await expect(canvas.getByTestId("zoom-level")).toHaveText(`${await attr(viewer, "data-zoom")}%`);

  // Fit: back to 100% with the whole model in view.
  await canvas.getByRole("button", { name: "Zoom" }).click();
  await canvas.getByRole("menuitem", { name: "Zoom to fit" }).click();
  await expect(viewer).toHaveAttribute("data-zoom", "100");
  await expect(viewer).toHaveAttribute("data-fits", "true");
});

test("the zoom menu zooms in, out and to presets", async ({ page, request }) => {
  const { viewer, canvas } = await openCanvas(page, request);
  const zoom = canvas.getByRole("button", { name: "Zoom" });
  await zoom.click();
  await canvas.getByRole("menuitem", { name: "Zoom in" }).click();
  await expect(viewer).toHaveAttribute("data-zoom", "125");
  await canvas.getByRole("menuitem", { name: "Zoom out" }).click();
  await expect(viewer).toHaveAttribute("data-zoom", "100");
  await canvas.getByRole("menuitem", { name: "400%" }).click();
  await expect(viewer).toHaveAttribute("data-zoom", "400");
  await expect(viewer).toHaveAttribute("data-fits", "false");
  await expect(canvas.getByTestId("zoom-level")).toHaveText("400%");
  await zoom.click();
  await canvas.getByRole("menuitem", { name: "50%" }).click();
  await expect(viewer).toHaveAttribute("data-zoom", "50");
  await expect(viewer).toHaveAttribute("data-fits", "true");
});

// The view cube itself is covered in view-cube.spec.ts.

test("mm/in switches the displayed size", async ({ page, request }) => {
  const { canvas } = await openCanvas(page, request);
  // The units switch is in View settings.
  await canvas.getByRole("button", { name: "View settings" }).click();
  const units = canvas.getByRole("radiogroup", { name: "Units" });
  const size = canvas.getByTestId("model-size");
  await expect(units.getByRole("radio", { name: "mm" })).toHaveAttribute("aria-checked", "true");
  await expect(size).toHaveText(/^[\d.]+ × [\d.]+ × [\d.]+ mm$/);
  const mm = (await size.textContent())!.split(" × ").map((v) => parseFloat(v));

  await units.getByRole("radio", { name: "in" }).click();
  await expect(size).toHaveText(/ in$/);
  const inches = (await size.textContent())!.split(" × ").map((v) => parseFloat(v));
  mm.forEach((v, i) => expect(inches[i]).toBeCloseTo(v / 25.4, 2));

  await units.getByRole("radio", { name: "mm" }).click();
  await expect(size).toHaveText(/ mm$/);
});

/** A downloaded export file is what its format says (the backend tests re-import each with a CAD or mesh reader). */
function expectValid(name: string, body: Buffer) {
  const text = body.toString("latin1");
  const ext = name.split(".").pop();
  if (ext === "step") expect(text.startsWith("ISO-10303-21;") && text.trimEnd().endsWith("END-ISO-10303-21;")).toBe(true);
  else if (ext === "stl") expect(body.length).toBe(84 + 50 * body.readUInt32LE(80));
  else if (ext === "3mf") expect(text.startsWith("PK") && text.includes("3D/3dmodel.model")).toBe(true);
  else if (ext === "glb") {
    expect(text.slice(0, 4)).toBe("glTF");
    expect(body.readUInt32LE(8)).toBe(body.length);
    const json = JSON.parse(body.subarray(20, 20 + body.readUInt32LE(12)).toString("utf8"));
    expect(json.asset.version).toBe("2.0");
  } else if (ext === "obj") expect(text).toMatch(/^o .+$[\s\S]*^v -?\d[\s\S]*^f \d/m);
  else if (ext === "dxf") expect(text.includes("SECTION") && text.trimEnd().endsWith("EOF")).toBe(true);
  else if (ext === "pdf") expect(text.startsWith("%PDF-") && text.trimEnd().endsWith("%%EOF")).toBe(true);
  else if (ext === "py") expect(text).toMatch(/^def build\(/m);
  else if (ext === "json") expect(typeof JSON.parse(text)).toBe("object");
  else if (ext === "md") {
    expect(text).toContain(`# Build note: ${BUILD.id}`);
    for (const r of CHECKS.results) expect(text).toContain(`| ${r.id} |`);
  } else throw new Error(`unexpected export file ${name}`);
}

test("export menu: STEP, the PDF drawing, source code and the build note download and are valid", async ({ page, request }) => {
  test.setTimeout(90_000);
  const { canvas } = await openCanvas(page, request);
  await canvas.getByRole("button", { name: "Export", exact: true }).click();
  const panel = canvas.getByRole("region", { name: "Export" });
  await expect(panel.getByRole("checkbox", { name: "STEP" })).toBeChecked();
  await expect(panel.getByRole("checkbox", { name: "PDF drawing" })).not.toBeChecked();
  await expect(panel.getByRole("checkbox", { name: "Source code" })).not.toBeChecked();
  const note = panel.getByRole("checkbox", { name: "Include build note" });
  await expect(note).toBeChecked();
  await expect(panel.getByTestId("export-note-summary")).toContainText(`${CHECKS.summary.untested} not tested`);
  await expect(panel.getByRole("checkbox")).toHaveCount(4);

  // Nothing checked: Download is disabled and says why.
  for (const box of await panel.getByRole("checkbox").all()) await box.uncheck();
  const download = panel.getByRole("button", { name: /^Download/ });
  await expect(download).toBeDisabled();
  await expect(download).toHaveAttribute("title", /format/);

  for (const box of await panel.getByRole("checkbox").all()) await box.check();
  const downloads: { name: string; body: Buffer }[] = [];
  page.on("download", async (d) => {
    const file = await d.path();
    downloads.push({ name: d.suggestedFilename(), body: readFileSync(file) });
  });
  // STEP, the PDF drawing, the source's code and parameters, and the build note.
  await expect(download).toHaveText("Download 5 files");
  await download.click();
  await expect(panel.getByRole("list", { name: "Exported files" }).getByRole("link")).toHaveCount(5, { timeout: 60_000 });
  await expect.poll(() => downloads.length, { timeout: 30_000 }).toBe(5);
  for (const d of downloads) expectValid(d.name, d.body);
});

test("export menu downloads only the checked formats, and Cancel closes it", async ({ page, request }) => {
  const { canvas } = await openCanvas(page, request);
  const exportButton = canvas.getByRole("button", { name: "Export", exact: true });
  await exportButton.click();
  const panel = canvas.getByRole("region", { name: "Export" });
  await panel.getByRole("checkbox", { name: "Include build note" }).uncheck();
  const checked = await panel.getByRole("checkbox", { checked: true }).count();
  expect(checked).toBeGreaterThan(0);
  const download = panel.getByRole("button", { name: /^Download/ });
  const files = Number((await download.textContent())!.match(/\d+/)![0]);
  const posted = page.waitForRequest((r) => r.method() === "POST" && r.url().endsWith("/exports"));
  await download.click();
  const formats = (await posted).postDataJSON().formats as string[];
  expect(formats).toHaveLength(checked);
  expect(formats).not.toContain("build_note");
  await expect(panel.getByRole("list", { name: "Exported files" }).getByRole("link")).toHaveCount(files, {
    timeout: 60_000,
  });
  await panel.getByRole("button", { name: "Cancel" }).click();
  await expect(panel).toBeHidden();
  await expect(exportButton).toHaveAttribute("aria-expanded", "false");
});

test("the Constraints panel shows the build's check summary, failing constraints first", async ({ page, request }) => {
  const { canvas } = await openCanvas(page, request);
  await canvas.getByRole("toolbar", { name: "Canvas controls" }).getByRole("button", { name: "Constraints" }).click();
  const s = CHECKS.summary;
  const na = s.not_applicable ? ` · ${s.not_applicable} not applicable` : "";
  await expect(canvas.getByTestId("checks-summary")).toHaveText(`${s.pass} pass · ${s.fail} fail · ${s.untested} untested${na}`);
  // One row per failing constraint (or check), however many places fail.
  const failing = new Set(CHECKS.results.filter((r) => r.status === "fail").map((r) => r.standard ?? r.check));
  const rows = canvas.getByRole("list", { name: "Constraints" }).getByTestId("constraint-item");
  await expect(rows.and(canvas.locator('[data-status="fail"]'))).toHaveCount(failing.size);
  await expect(rows.first()).toHaveAttribute("data-status", "fail");
});

test("code shows the selected build's CAD source", async ({ page, request }) => {
  const SOURCE = fixture("build-source.json") as { build_id: string; backend: string; filename: string; code: string };
  const { canvas } = await openCanvas(page, request);
  await canvas.getByRole("button", { name: "Code" }).click();
  const code = canvas.getByRole("region", { name: "Code" });
  await expect(code.getByTestId("code-file")).toHaveText(`${SOURCE.filename} · ${SOURCE.backend} · ${REVISION}`);
  const editor = code.getByRole("textbox", { name: "Generator code" });
  await expect(editor).toHaveAttribute("data-build", BUILD.id);
  await expect(editor).toHaveValue(SOURCE.code);
  await page.keyboard.press("Escape");
  await expect(code).toBeHidden();
});

test("view settings toggle edges and grid", async ({ page, request }) => {
  const { canvas } = await openCanvas(page, request);
  const gl = canvas.locator("canvas");
  await canvas.getByRole("button", { name: "View settings" }).click();
  const settings = canvas.getByRole("region", { name: "View settings" });
  for (const name of ["Edges", "Grid"]) {
    const box = settings.getByRole("checkbox", { name });
    await expect(box).toBeChecked();
    const before = await gl.screenshot();
    await box.uncheck();
    await expect(box).not.toBeChecked();
    await expect.poll(async () => (await gl.screenshot()).equals(before)).toBe(false);
    await box.check();
  }
});

// Zooming out must never clip the model or draw the grid over it.

/** The 3D view only: the overlays (toolbar, view cube, labels) are hidden while capturing. */
const VIEW_ONLY = "[data-testid=viewer] ~ * { visibility: hidden !important; }";

const shoot = async (viewer: Locator) =>
  (await viewer.screenshot({ style: VIEW_ONLY, animations: "disabled" })).toString("base64");

type Analysis = {
  /** Model pixels' bounding box [x0, y0, x1, y1] in device pixels, or null if none. */
  bbox: [number, number, number, number] | null;
  /** Pixels inside the model's silhouette that the grid changed. */
  covered: number;
  width: number;
};

/**
 * Compares screenshots of the view with the grid off (`plain`) and on (`gridded`) in the page,
 * which decodes PNGs without an image library. Model pixels are those that differ from the
 * background colour with the grid off.
 */
function analyse(page: Page, plain: string, gridded: string, background: number[]): Promise<Analysis> {
  return page.evaluate(
    async ({ plain, gridded, background }) => {
      const pixels = async (b64: string) => {
        const bitmap = await createImageBitmap(await (await fetch(`data:image/png;base64,${b64}`)).blob());
        const ctx = new OffscreenCanvas(bitmap.width, bitmap.height).getContext("2d")!;
        ctx.drawImage(bitmap, 0, 0);
        return ctx.getImageData(0, 0, bitmap.width, bitmap.height);
      };
      const a = await pixels(plain);
      const b = await pixels(gridded);
      const { width, height } = a;
      const d = (x: Uint8ClampedArray, i: number, c: ArrayLike<number>, j = 0) =>
        Math.max(Math.abs(x[i * 4] - c[j]), Math.abs(x[i * 4 + 1] - c[j + 1]), Math.abs(x[i * 4 + 2] - c[j + 2]));
      const model = new Uint8Array(width * height);
      let [x0, y0, x1, y1] = [width, height, -1, -1];
      for (let i = 0; i < width * height; i++) {
        if (d(a.data, i, background) <= 6) continue;
        model[i] = 1;
        const x = i % width;
        const y = (i - x) / width;
        x0 = Math.min(x0, x);
        y0 = Math.min(y0, y);
        x1 = Math.max(x1, x);
        y1 = Math.max(y1, y);
      }
      // Inside the silhouette (one pixel in from its antialiased edge) the grid changes nothing.
      let covered = 0;
      for (let y = 1; y < height - 1; y++)
        for (let x = 1; x < width - 1; x++) {
          const i = y * width + x;
          if (!(model[i] && model[i - 1] && model[i + 1] && model[i - width] && model[i + width])) continue;
          if (d(b.data, i, a.data, i * 4) > 8) covered++;
        }
      const bbox: [number, number, number, number] | null = x1 < 0 ? null : [x0, y0, x1, y1];
      return { bbox, covered, width };
    },
    { plain, gridded, background },
  );
}

async function setGrid(canvas: Locator, on: boolean) {
  await canvas.getByRole("button", { name: "View settings" }).click();
  await canvas.getByRole("region", { name: "View settings" }).getByRole("checkbox", { name: "Grid" }).setChecked(on);
  await canvas.getByRole("button", { name: "View settings" }).click();
}

/**
 * At the current camera: the model is drawn in full (its pixels span the on-screen box the
 * viewer reports, so no clip plane cuts it short) and the grid changes no pixel inside it.
 */
async function expectModelIntact(canvas: Locator, viewer: Locator, background: number[], label: string) {
  const [x, y, w, h] = (await attr(viewer, "data-model-rect")).split(",").map(Number);
  expect(w * h, `${label}: model on screen`).toBeGreaterThan(0);
  await setGrid(canvas, false);
  const plain = await shoot(viewer);
  await setGrid(canvas, true);
  const gridded = await shoot(viewer);
  const { bbox, covered, width } = await analyse(viewer.page(), plain, gridded, background);
  expect(bbox, `${label}: model pixels drawn`).not.toBeNull();
  const [x0, y0, x1, y1] = bbox!;
  const scale = width / (await viewer.boundingBox())!.width;
  const tol = 3 * scale;
  expect(Math.abs(x0 - x * scale), `${label}: left edge`).toBeLessThanOrEqual(tol);
  expect(Math.abs(y0 - y * scale), `${label}: top edge`).toBeLessThanOrEqual(tol);
  expect(Math.abs(x1 + 1 - (x + w) * scale), `${label}: right edge`).toBeLessThanOrEqual(tol);
  expect(Math.abs(y1 + 1 - (y + h) * scale), `${label}: bottom edge`).toBeLessThanOrEqual(tol);
  expect(covered, `${label}: grid pixels inside the model`).toBe(0);
}

const zoomOf = async (viewer: Locator) => Number(await attr(viewer, "data-zoom"));

test("the model stays whole and in front of the grid at every zoom level", async ({ page, request }) => {
  test.setTimeout(300_000);
  await page.setViewportSize({ width: 1440, height: 900 });
  const { viewer, canvas } = await openCanvas(page, request);
  const min = Number(await attr(viewer, "data-zoom-min"));
  const max = Number(await attr(viewer, "data-zoom-max"));
  expect(min).toBeGreaterThan(0);
  expect(max).toBeGreaterThan(min);

  // Background: a corner pixel with the grid off at the fitted view, where the model is not.
  await setGrid(canvas, false);
  const background = await page.evaluate(async (b64) => {
    const bitmap = await createImageBitmap(await (await fetch(`data:image/png;base64,${b64}`)).blob());
    const ctx = new OffscreenCanvas(bitmap.width, bitmap.height).getContext("2d")!;
    ctx.drawImage(bitmap, 0, 0);
    return Array.from(ctx.getImageData(0, 0, 1, 1).data.slice(0, 3));
  }, await shoot(viewer));
  await setGrid(canvas, true);

  const box = (await viewer.boundingBox())!;
  const wheel = async (dy: number) => {
    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
    await page.mouse.wheel(0, dy);
  };

  const cube = canvas.getByTestId("view-cube");
  for (const view of ["Home", "Bottom"]) {
    // The bottom face points away in the home view: the chevron below turns to it from Front.
    if (view === "Home") await cube.getByRole("button", { name: "Home view" }).click();
    else {
      await cube.getByRole("button", { name: "Front", exact: true }).click();
      await expect(viewer).toHaveAttribute("data-view", "front");
      await cube.getByRole("button", { name: "Rotate to the face below" }).click();
    }
    await expect(viewer).toHaveAttribute("data-view", view === "Home" ? "iso" : "bottom");
    // The wheel (the path the bug was on) stops at the maximum...
    for (let i = 0; i < 12 && (await zoomOf(viewer)) < max; i++) await wheel(-2000);
    await expect.poll(() => zoomOf(viewer)).toBe(max);
    // ...then steps out in fixed steps to the minimum, where it stops.
    for (let step = 0; step < 60; step++) {
      const zoom = await zoomOf(viewer);
      await expectModelIntact(canvas, viewer, background, `${view} ${zoom}%`);
      if (zoom === min) break;
      await wheel(400);
      await expect.poll(() => zoomOf(viewer)).toBeLessThan(zoom);
    }
    expect(await zoomOf(viewer)).toBe(min);
    await wheel(2000);
    await page.waitForTimeout(100);
    expect(await zoomOf(viewer)).toBe(min);
  }
});

test("the zoom readout says the zoom limits and the menu stops at them", async ({ page, request }) => {
  const { viewer, canvas } = await openCanvas(page, request);
  const min = Number(await attr(viewer, "data-zoom-min"));
  const max = Number(await attr(viewer, "data-zoom-max"));
  const readout = canvas.getByRole("button", { name: "Zoom" });
  await expect(readout).toHaveAttribute("title", `Zoom ${min}% to ${max}%`);
  await readout.click();
  for (let i = 0; i < 40; i++) await canvas.getByRole("menuitem", { name: "Zoom out" }).click();
  await expect(viewer).toHaveAttribute("data-zoom", String(min));
  await expect(canvas.getByTestId("zoom-level")).toHaveText(`${min}%`);
  for (let i = 0; i < 40; i++) await canvas.getByRole("menuitem", { name: "Zoom in" }).click();
  await expect(viewer).toHaveAttribute("data-zoom", String(max));
  await expect(canvas.getByTestId("zoom-level")).toHaveText(`${max}%`);
});

test("the canvas matches the reference screenshots at minimum, default and maximum zoom", async ({ page, request }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  const { viewer, canvas } = await openCanvas(page, request);
  const zoom = canvas.getByRole("button", { name: "Zoom" });
  const shot = { maxDiffPixelRatio: 0.02, animations: "disabled" as const };
  await expect(canvas).toHaveScreenshot("canvas-zoom-default.png", shot);
  await zoom.click();
  for (let i = 0; i < 40; i++) await canvas.getByRole("menuitem", { name: "Zoom out" }).click();
  await expect(viewer).toHaveAttribute("data-zoom", await attr(viewer, "data-zoom-min"));
  await zoom.click();
  await expect(canvas).toHaveScreenshot("canvas-zoom-min.png", shot);
  await zoom.click();
  for (let i = 0; i < 40; i++) await canvas.getByRole("menuitem", { name: "Zoom in" }).click();
  await expect(viewer).toHaveAttribute("data-zoom", await attr(viewer, "data-zoom-max"));
  await zoom.click();
  await expect(canvas).toHaveScreenshot("canvas-zoom-max.png", shot);
});
