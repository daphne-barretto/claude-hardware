import { readFileSync } from "node:fs";
import path from "node:path";
import { expect, test, type APIRequestContext, type Locator, type Page } from "@playwright/test";

// Inspection tools: section cut and the Assembled /
// Exploded / Print layouts, against the mock session API. The board, the check and the mesh
// come from demos/b-parasite/ and fixtures/, never from here.
test.skip(!!process.env.E2E_BASE_URL, "the mock API runs only with the local dev servers");

const root = path.join(__dirname, "../..");
const read = (file: string) => JSON.parse(readFileSync(path.join(root, file), "utf8"));
const SESSION = read("fixtures/session.json") as { prompt: string };
const BUILD = read("fixtures/build.json") as { board: { name: string; revision: string } };
type Component = { ref: string; role: string | null; side: "top" | "bottom"; position: [number, number]; rotation: number; size: [number, number]; height: number };
const BOARD = read(`demos/b-parasite/${BUILD.board.name}-${BUILD.board.revision}.json`) as {
  pcb: { thickness: number };
  components: Component[];
};
const label = (c: Component) => (c.role ? `${c.ref} · ${c.role}` : c.ref);

/** A component body's corners in x-y and its z range, as backend/app/boards/SCHEMA.md places it. */
function body(c: Component) {
  const r = (c.rotation * Math.PI) / 180;
  const [w, l] = c.size;
  const corners = [
    [-w / 2, -l / 2],
    [w / 2, -l / 2],
    [w / 2, l / 2],
    [-w / 2, l / 2],
  ].map(([x, y]) => [c.position[0] + x * Math.cos(r) - y * Math.sin(r), c.position[1] + x * Math.sin(r) + y * Math.cos(r)]);
  const h = Math.max(c.height, 1e-3);
  const t = BOARD.pcb.thickness;
  return { corners, z: c.side === "top" ? [t, t + h] : [-h, 0] };
}

async function openCanvas(page: Page, request: APIRequestContext) {
  const session = await (await request.post("/api/sessions", { data: {} })).json();
  await request.post(`/api/sessions/${session.id}/messages`, { data: { text: SESSION.prompt } });
  await expect
    .poll(async () => (await (await request.get(`/api/sessions/${session.id}`)).json()).status, { timeout: 30_000 })
    .not.toBe("running");
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto(`/session/${session.id}`);
  await expect(page.locator("[data-shell-ready=true]")).toBeAttached({ timeout: 30_000 });
  const viewer = page.getByTestId("viewer");
  await expect(viewer).toHaveAttribute("data-state", "ready", { timeout: 30_000 });
  await expect(viewer).toHaveAttribute("data-board", `${BUILD.board.name} ${BUILD.board.revision}`);
  const canvas = page.getByRole("region", { name: "Canvas" });
  return { viewer, canvas, tools: canvas.getByRole("toolbar", { name: "Canvas controls" }) };
}

const settled = (viewer: Locator) => expect(viewer).toHaveAttribute("data-animating", "false");
const shot = { maxDiffPixelRatio: 0.02, animations: "disabled" as const };

test("a section through any component cuts the model there and shows that component's clearance", async ({ page, request }) => {
  const { viewer, canvas, tools } = await openCanvas(page, request);
  const gl = canvas.locator("canvas");
  const whole = await gl.screenshot();

  await tools.getByRole("button", { name: "Section cut" }).click();
  await expect(tools.getByRole("button", { name: "Section cut" })).toHaveAttribute("aria-pressed", "true");
  const panel = canvas.getByRole("region", { name: "Section" });
  await expect(panel.getByTestId("section-clearance")).toBeVisible();
  const first = await panel.getByRole("button", { name: "Section through" }).textContent();
  await expect.poll(async () => (await gl.screenshot()).equals(whole)).toBe(false);

  const clearances: Record<string, number> = {};
  for (const c of BOARD.components) {
    await panel.getByRole("button", { name: "Section through" }).click();
    await panel.getByRole("menu", { name: "Section through" }).getByRole("menuitemradio", { name: label(c), exact: true }).click();
    await expect(panel.getByRole("button", { name: "Section through" })).toHaveText(label(c));
    const cx = body(c).corners.reduce((s, p) => s + p[0], 0) / 4;
    await expect(viewer).toHaveAttribute("data-section", new RegExp(`^${c.ref}:x:`));
    expect(Number((await viewer.getAttribute("data-section"))!.split(":")[2])).toBeCloseTo(cx, 6);
    await expect(panel.getByTestId("section-plane")).toHaveText(`Section at x = ${cx.toFixed(1)} mm`);
    const clearance = panel.getByTestId("section-clearance");
    await expect(clearance).toContainText(`from ${c.ref} to the `);
    clearances[c.ref] = Number(await clearance.getAttribute("data-value"));
    expect(clearances[c.ref]).toBeGreaterThanOrEqual(0);
  }
  // It started at the component closest to the enclosure.
  const closest = Object.entries(clearances).reduce((a, b) => (b[1] < a[1] ? b : a))[0];
  expect(first).toBe(label(BOARD.components.find((c) => c.ref === closest)!));
  await panel.getByRole("button", { name: "Section through" }).click();
  await panel.getByRole("menuitemradio", { name: first!, exact: true }).click();

  // Every axis cuts through the component's centre; the clearance is drawn on the model.
  for (const axis of ["y", "z", "x"]) {
    await panel.getByRole("radiogroup", { name: "Section axis" }).getByRole("radio", { name: axis.toUpperCase() }).click();
    await expect(viewer).toHaveAttribute("data-section", new RegExp(`^${closest}:${axis}:`));
  }
  if (clearances[closest] > 0) await expect(canvas.getByTestId("dimension-label")).toHaveText(`${clearances[closest].toFixed(2)} mm`);
  await page.mouse.move(5, 5);
  await expect(canvas).toHaveScreenshot("section.png", shot);

  await tools.getByRole("button", { name: "Section cut" }).click();
  await expect(panel).toBeHidden();
  await expect(viewer).toHaveAttribute("data-section", "");
});

test("each layout works: assembled, exploded and print layout", async ({ page, request }) => {
  const { viewer, canvas } = await openCanvas(page, request);
  const layouts = canvas.getByRole("radiogroup", { name: "Layout" });
  const rect = async () => (await viewer.getAttribute("data-model-rect"))!.split(",").map(Number);
  const look = async (face: string) => {
    // The view shortcuts (1 front, 2 top), with focus on a canvas control, not a text field.
    await canvas.getByRole("radiogroup", { name: "Layout" }).getByRole("radio", { checked: true }).focus();
    await page.keyboard.press(face === "Front" ? "1" : "2");
    await expect(viewer).toHaveAttribute("data-view", face.toLowerCase());
    await settled(viewer);
  };
  await expect(layouts.getByRole("radio", { name: "Assembled" })).toHaveAttribute("aria-checked", "true");
  const sizeText = await canvas.getByTestId("model-size").textContent();

  const pick = async (name: string, value: string, screenshot = true) => {
    await layouts.getByRole("radio", { name }).click();
    await expect(layouts.getByRole("radio", { name })).toHaveAttribute("aria-checked", "true");
    await expect(viewer).toHaveAttribute("data-layout", value);
    await expect(viewer).toHaveAttribute("data-fits", "true");
    await settled(viewer);
    if (!screenshot) return;
    await page.mouse.move(5, 5);
    await expect(canvas).toHaveScreenshot(`layout-${value}.png`, shot);
  };

  // Seen from the front, exploded is taller for its width than assembled: the parts are apart along Z.
  await pick("Exploded", "exploded");
  await look("Front");
  const [, , ew, eh] = await rect();
  await pick("Assembled", "assembled", false);
  await look("Front");
  const [, , aw, ah] = await rect();
  expect(eh / ew).toBeGreaterThan((ah / aw) * 2);

  // Seen from the top, the print layout is wider for its depth: the parts lie side by side on the bed.
  await look("Top");
  const [, , tw, th] = await rect();
  await pick("Print layout", "print");
  await look("Top");
  const [, , pw, ph] = await rect();
  expect(pw / ph).toBeGreaterThan((tw / th) * 1.5);

  // Back to assembled: the enclosure as designed, the same size.
  await pick("Assembled", "assembled");
  await look("Front");
  await expect(canvas.getByTestId("model-size")).toHaveText(sizeText!);
});
