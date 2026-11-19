import { readFileSync } from "node:fs";
import path from "node:path";
import { expect, test, type APIRequestContext, type Locator, type Page } from "@playwright/test";
import { pickCheck } from "./fixtures/constraints";

// Inspection tools: section cut, x-ray, measure with snapping, and the Assembled /
// Exploded / Print layouts, against the mock session API. The board, the check and the mesh
// come from demos/b-parasite/ and fixtures/, never from here.
test.skip(!!process.env.E2E_BASE_URL, "the mock API runs only with the local dev servers");

type Vec3 = [number, number, number];
const root = path.join(__dirname, "../..");
const read = (file: string) => JSON.parse(readFileSync(path.join(root, file), "utf8"));
const SESSION = read("fixtures/session.json") as { prompt: string };
const BUILD = read("fixtures/build.json") as { board: { name: string; revision: string } };
type Component = { ref: string; role: string | null; side: "top" | "bottom"; position: [number, number]; rotation: number; size: [number, number]; height: number };
const BOARD = read(`demos/b-parasite/${BUILD.board.name}-${BUILD.board.revision}.json`) as {
  pcb: { thickness: number };
  components: Component[];
};
type CheckResult = { id: string; check: string; measured?: { value: number | string } | null; location?: { point: Vec3; faces?: string[] } | null };
const CLEARANCE = (read("fixtures/checks.json") as { results: CheckResult[] }).results.find((r) => r.check === "component_clearance")!;
// The check reports the point on the enclosure where the distance occurs; the component it
// measures from is the one whose body is closest to that point.
const CHECKED = BOARD.components.reduce((a, b) => (gap(b, CLEARANCE.location!.point) < gap(a, CLEARANCE.location!.point) ? b : a));
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
/** The distance from a point to a component's body. */
function gap(c: Component, p: Vec3) {
  const r = (c.rotation * Math.PI) / 180;
  const [dx, dy] = [p[0] - c.position[0], p[1] - c.position[1]];
  const local = [dx * Math.cos(r) + dy * Math.sin(r), -dx * Math.sin(r) + dy * Math.cos(r)];
  const { z } = body(c);
  const out = [Math.abs(local[0]) - c.size[0] / 2, Math.abs(local[1]) - c.size[1] / 2, Math.max(z[0] - p[2], p[2] - z[1])];
  return Math.hypot(...out.map((v) => Math.max(v, 0)));
}
const inside = (p: number[], poly: number[][]) =>
  poly.every((a, i) => {
    const b = poly[(i + 1) % poly.length];
    return (b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0]) >= 0;
  });

/** The triangles of one face of the fixture mesh, in board coordinates. */
function faceTriangles(faceId: string): Vec3[][] {
  const g = read("fixtures/enclosure.gltf") as {
    nodes: { mesh?: number; extras?: { faces?: { id: string }[] } }[];
    meshes: { primitives: { mode?: number; attributes: Record<string, number>; indices?: number }[] }[];
    accessors: { bufferView: number; byteOffset?: number; componentType: number; count: number; type: string }[];
    bufferViews: { byteOffset?: number }[];
    buffers: { uri: string }[];
  };
  const bytes = Buffer.from(g.buffers[0].uri.split(",")[1], "base64");
  const acc = (i: number) => {
    const a = g.accessors[i];
    const n = a.count * ({ SCALAR: 1, VEC3: 3 } as Record<string, number>)[a.type];
    const view = new DataView(bytes.buffer, bytes.byteOffset + (g.bufferViews[a.bufferView].byteOffset ?? 0) + (a.byteOffset ?? 0));
    return Array.from({ length: n }, (_, k) =>
      a.componentType === 5126 ? view.getFloat32(k * 4, true) : a.componentType === 5123 ? view.getUint16(k * 2, true) : view.getUint32(k * 4, true),
    );
  };
  for (const node of g.nodes) {
    const index = node.extras?.faces?.findIndex((f) => f.id === faceId) ?? -1;
    if (index < 0 || node.mesh === undefined) continue;
    const prim = g.meshes[node.mesh].primitives.find((p) => (p.mode ?? 4) === 4)!;
    const pos = acc(prim.attributes.POSITION);
    const ids = acc(prim.attributes._FACE_ID);
    const idx = acc(prim.indices!);
    const out: Vec3[][] = [];
    for (let i = 0; i < idx.length; i += 3)
      if (ids[idx[i]] === index) out.push([idx[i], idx[i + 1], idx[i + 2]].map((v) => pos.slice(v * 3, v * 3 + 3) as Vec3));
    return out;
  }
  throw new Error(`no face ${faceId} in the fixture mesh`);
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

/** Where a board point is in the page, from the viewer's board-to-clip matrix. */
async function toPage(viewer: Locator, p: Vec3): Promise<[number, number]> {
  const m = ((await viewer.getAttribute("data-board-to-clip")) ?? "").split(",").map(Number);
  const [x, y, z] = p;
  const w = m[3] * x + m[7] * y + m[11] * z + m[15];
  const cx = (m[0] * x + m[4] * y + m[8] * z + m[12]) / w;
  const cy = (m[1] * x + m[5] * y + m[9] * z + m[13]) / w;
  const box = (await viewer.boundingBox())!;
  return [box.x + ((cx + 1) / 2) * box.width, box.y + ((1 - cy) / 2) * box.height];
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

test("the measure tool snaps to the component and matches the clearance check's measured value", async ({ page, request }) => {
  const { viewer, canvas, tools } = await openCanvas(page, request);
  const expected = Number(CLEARANCE.measured!.value);
  const face = CLEARANCE.location!.faces![0];

  // A section through the checked component, along the axis that looks square on at the face
  // it is checked against: each section keeps the side away from the view that looks at the cut.
  const tris = faceTriangles(face);
  const b = body(CHECKED);
  const centre: Vec3 = [b.corners.reduce((s, p) => s + p[0], 0) / 4, b.corners.reduce((s, p) => s + p[1], 0) / 4, (b.z[0] + b.z[1]) / 2];
  const normal = (t: Vec3[]) => {
    const u = [0, 1, 2].map((k) => t[1][k] - t[0][k]);
    const v = [0, 1, 2].map((k) => t[2][k] - t[0][k]);
    return [u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2], u[0] * v[1] - u[1] * v[0]];
  };
  const SIDES = [
    { axis: "X", k: 0, keep: -1, view: "Right" },
    { axis: "Y", k: 1, keep: 1, view: "Front" },
    { axis: "Z", k: 2, keep: -1, view: "Top" },
  ];
  const kept = (side: (typeof SIDES)[number], p: number[]) => (p[side.k] - centre[side.k]) * side.keep >= 0;
  // How much of the face, by area, is kept and turned towards the view.
  const facing = (side: (typeof SIDES)[number]) =>
    tris.reduce((sum, t) => {
      const n = normal(t);
      const mid = [0, 1, 2].map((k) => (t[0][k] + t[1][k] + t[2][k]) / 3);
      return sum + (kept(side, mid) ? Math.max(0, -side.keep * n[side.k]) : 0);
    }, 0);
  const side = SIDES.reduce((a, c) => (facing(c) > facing(a) ? c : a));
  expect(facing(side)).toBeGreaterThan(0);

  await tools.getByRole("button", { name: "Section cut" }).click();
  const section = canvas.getByRole("region", { name: "Section" });
  await section.getByRole("button", { name: "Section through" }).click();
  await section.getByRole("menuitemradio", { name: label(CHECKED), exact: true }).click();
  await section.getByRole("radio", { name: side.axis }).click();
  await expect(viewer).toHaveAttribute("data-section", new RegExp(`^${CHECKED.ref}:${side.axis.toLowerCase()}:`));
  await canvas.getByRole("button", { name: side.view, exact: true }).click();
  await expect(viewer).toHaveAttribute("data-view", side.view.toLowerCase());
  await settled(viewer);
  // Close in on the point the check reports, from its place in the constraint's detail.
  await pickCheck(canvas, CLEARANCE, { close: true });
  await settled(viewer);
  await expect(viewer).toHaveAttribute("data-view", side.view.toLowerCase());
  const card = canvas.getByTestId("check-card");
  await card.getByRole("button", { name: "Close" }).click();
  await expect(card).toBeHidden();

  await page.keyboard.press("m");
  await expect(tools.getByRole("button", { name: "Measure" })).toHaveAttribute("aria-pressed", "true");
  // The check pins step aside while measuring, so a click on the model measures.
  await expect(canvas.getByTestId("check-pin")).toHaveCount(0);
  const measure = canvas.getByRole("region", { name: "Measure" });
  // A point of the component's cut face near the reported point, clear of its corners, so it
  // is in view.
  const at = CLEARANCE.location!.point;
  const grip = centre.map((v, k) => {
    if (k === side.k || k === 2) return v;
    const span = b.corners.map((q) => q[k]);
    return Math.min(Math.max(at[k], Math.min(...span) + 2), Math.max(...span) - 2);
  }) as Vec3;
  const pickComponent = async () => {
    await page.mouse.click(...(await toPage(viewer, grip)));
    await expect(measure.getByTestId("measure-from")).toHaveText(CHECKED.ref);
  };
  await pickComponent();

  // Points spread over the face, kept by the section, turned towards the view and not behind a
  // component or the board on the way to it, closest to the point the check reports first.
  const towards = [0, 0, 0];
  towards[side.k] = -side.keep;
  const hidden = (p: number[]) =>
    (side.k !== 2 && p[2] >= -0.01 && p[2] <= BOARD.pcb.thickness + 0.01) ||
    BOARD.components.some((c) => {
      const cb = body(c);
      // Does the way from p towards the camera pass through the body's kept part?
      if (side.k === 2) return inside(p, cb.corners) && cb.z[1] > p[2] && cb.z[0] < centre[2];
      const xs = cb.corners.map((q) => q[side.k]);
      const ahead = towards[side.k] > 0 ? Math.min(Math.max(...xs), centre[side.k]) > p[side.k] : Math.max(Math.min(...xs), centre[side.k]) < p[side.k];
      const os = cb.corners.map((q) => q[1 - side.k]);
      const zIn = p[2] >= cb.z[0] && p[2] <= cb.z[1];
      return ahead && zIn && p[1 - side.k] >= Math.min(...os) && p[1 - side.k] <= Math.max(...os);
    });
  const candidates = tris
    .filter((t) => -side.keep * normal(t)[side.k] > 0)
    .flatMap((t) =>
      [
        [1, 1, 1],
        [4, 1, 1],
        [1, 4, 1],
        [1, 1, 4],
      ].map((w) => [0, 1, 2].map((k) => (w[0] * t[0][k] + w[1] * t[1][k] + w[2] * t[2][k]) / 6) as Vec3),
    )
    .filter((p) => kept(side, p) && !hidden(p))
    .sort((p, q) => Math.hypot(...p.map((v, k) => v - at[k])) - Math.hypot(...q.map((v, k) => v - at[k])));
  expect(candidates.length).toBeGreaterThan(0);

  const result = measure.getByTestId("measure-result");
  let found = false;
  for (const p of candidates.slice(0, 8)) {
    await page.mouse.click(...(await toPage(viewer, p)));
    await expect(result).toBeVisible();
    if ((await result.getAttribute("data-to")) === face) {
      found = true;
      break;
    }
    await measure.getByRole("button", { name: "Clear measurement" }).click();
    await pickComponent();
  }
  expect(found, `a click on ${face} measures to it`).toBe(true);
  await expect(result).toHaveAttribute("data-from", CHECKED.ref);
  await expect(result).toHaveAttribute("data-to", face);
  const measured = Number(await result.getAttribute("data-value"));
  expect(Math.abs(measured - expected)).toBeLessThanOrEqual(0.05);
  await expect(result).toContainText(`${measured.toFixed(2)} mm`);
  await page.mouse.move(5, 5);
  await expect(canvas).toHaveScreenshot("measure.png", shot);

  // Near a corner, it measures from the corner.
  await measure.getByRole("button", { name: "Clear measurement" }).click();
  // The kept corner of the component closest to the reported point, clicked a little inside it.
  const corner = b.corners
    .flatMap((q) => b.z.map((z) => [q[0], q[1], z] as Vec3))
    .filter((q) => kept(side, q))
    .reduce((p, q) => (Math.hypot(...q.map((v, k) => v - at[k])) < Math.hypot(...p.map((v, k) => v - at[k])) ? q : p));
  const [cx, cy] = await toPage(viewer, corner);
  const [gx, gy] = await toPage(viewer, grip);
  const step = 3 / Math.hypot(gx - cx, gy - cy);
  await page.mouse.click(cx + (gx - cx) * step, cy + (gy - cy) * step);
  await expect(measure.getByTestId("measure-from")).toHaveText(`Corner of ${CHECKED.ref}`);

  // Escape clears it; S goes back to selecting.
  await page.keyboard.press("Escape");
  await expect(measure.getByTestId("measure-from")).toBeHidden();
  await page.keyboard.press("s");
  await expect(measure).toBeHidden();
  await expect(viewer).toHaveAttribute("data-measuring", "false");
  await expect(canvas.getByTestId("check-pin").first()).toBeVisible();
});

test("x-ray shows the board and its components through the enclosure", async ({ page, request }) => {
  const { viewer, canvas, tools } = await openCanvas(page, request);
  const gl = canvas.locator("canvas");
  const solid = await gl.screenshot();
  await tools.getByRole("button", { name: "X-ray" }).click();
  await expect(viewer).toHaveAttribute("data-xray", "true");
  await expect.poll(async () => (await gl.screenshot()).equals(solid)).toBe(false);
  await page.mouse.move(5, 5);
  await expect(canvas).toHaveScreenshot("xray.png", shot);
  await tools.getByRole("button", { name: "X-ray" }).click();
  await expect(viewer).toHaveAttribute("data-xray", "false");
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
