import { readFileSync } from "node:fs";
import path from "node:path";
import { PIN_FILTER_KEY } from "../src/components/canvas/pinVisibility";
import { expect, test, type APIRequestContext, type Locator, type Page } from "@playwright/test";
import { answerOpenQuestion } from "./inventory/ui-inventory";

// The exploded view against the mock session API (docs/architecture.md). Parts, faces
// and check locations come from the fixture files, never from here.
test.skip(!!process.env.E2E_BASE_URL, "the mock API runs only with the local dev servers");
// Every check's pin, not only the failed ones the canvas shows by default (the "Check pins" filter).
test.beforeEach(({ page }) => page.addInitScript((key) => localStorage.setItem(key, "all"), PIN_FILTER_KEY));

type Point = [number, number, number];
type Gltf = {
  nodes: { mesh?: number; extras?: { part?: string; faces?: { id: string }[] } }[];
  meshes: { primitives: { attributes: { POSITION: number } }[] }[];
  accessors: { min?: number[]; max?: number[] }[];
};
type Result = { id: string; status: string; location?: { point: Point; part?: string | null } | null };
const read = (name: string) => JSON.parse(readFileSync(path.join(__dirname, "../../fixtures", name), "utf8"));
const MESH = read("enclosure.gltf") as Gltf;
const SESSION = read("session.json") as { prompt: string };
const CHECKS = read("checks.json") as { results: Result[] };
const ON_A_PART = CHECKS.results.filter((r) => r.status !== "untested" && r.location?.part);
/** Each part's faces and its assembled extent along board Z (vertices are in board coordinates). */
const PARTS = MESH.nodes.flatMap((n) => {
  if (!n.extras?.part || n.mesh === undefined) return [];
  const acc = MESH.meshes[n.mesh].primitives.map((p) => MESH.accessors[p.attributes.POSITION]);
  const zs = acc.flatMap((a) => [a.min![2], a.max![2]]);
  return [{ part: n.extras.part, faces: (n.extras.faces ?? []).map((f) => f.id), min: Math.min(...zs), max: Math.max(...zs) }];
});
const partOfFace = (id: string) => PARTS.find((p) => p.faces.includes(id))!;

async function openCanvas(page: Page, request: APIRequestContext) {
  const session = await (await request.post("/api/sessions", { data: {} })).json();
  await request.post(`/api/sessions/${session.id}/messages`, { data: { text: SESSION.prompt } });
  await expect
    .poll(async () => (await (await request.get(`/api/sessions/${session.id}/builds`)).json()).length, { timeout: 30_000 })
    .toBeGreaterThan(0);
  await expect
    .poll(async () => (await (await request.get(`/api/sessions/${session.id}`)).json()).status, { timeout: 30_000 })
    .not.toBe("running");
  await answerOpenQuestion(page, `/api/sessions/${session.id}`);
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto(`/session/${session.id}`);
  await expect(page.locator("[data-shell-ready=true]")).toBeAttached({ timeout: 30_000 });
  const viewer = page.getByTestId("viewer");
  await expect(viewer).toHaveAttribute("data-state", "ready", { timeout: 30_000 });
  const canvas = page.getByRole("region", { name: "Canvas" });
  // The check results load after the model: wait for their pins, so the picture is settled.
  await expect(canvas.getByTestId("check-pin")).toHaveCount(CHECKS.results.filter((r) => r.status !== "untested" && r.location).length);
  return {
    viewer,
    canvas,
    chat: page.getByRole("region", { name: "Chat" }),
    explode: canvas.getByRole("toolbar", { name: "Canvas controls" }).getByRole("button", { name: "Explode" }),
    slider: canvas.getByRole("slider", { name: "Explode amount" }),
  };
}

/** Each part's current offset along board +Z, from the viewer. */
async function offsets(viewer: Locator): Promise<Record<string, number>> {
  const raw = (await viewer.getAttribute("data-explode-offsets")) ?? "";
  return Object.fromEntries(
    raw
      .split(",")
      .filter(Boolean)
      .map((kv) => {
        const i = kv.lastIndexOf(":");
        return [kv.slice(0, i), Number(kv.slice(i + 1))];
      }),
  );
}

const pinAt = async (canvas: Locator, id: string) => {
  const box = (await canvas.locator(`[data-testid=check-pin][data-check="${id}"]`).boundingBox())!;
  return { x: box.x + box.width / 2, y: box.y + box.height / 2 };
};

test("the exploded view separates every part with a slider, and switching back is exact", async ({ page, request }) => {
  const { viewer, explode, slider } = await openCanvas(page, request);
  expect(PARTS.length).toBeGreaterThan(1);
  await expect(explode).toHaveAttribute("aria-pressed", "false");
  await expect(slider).toHaveCount(0);
  await expect(viewer).toHaveAttribute("data-explode", "0");
  // The parts panel floats over the view; its text anti-aliasing can shift by a level as the
  // compositor re-layers it, so the picture comparison masks it and covers the model only.
  const shot = () => viewer.screenshot({ mask: [page.getByRole("region", { name: "Parts" })] });
  const before = {
    rect: await viewer.getAttribute("data-model-rect"),
    shot: await shot(),
  };
  for (const d of Object.values(await offsets(viewer))) expect(d).toBe(0);

  await explode.click();
  await expect(explode).toHaveAttribute("aria-pressed", "true");
  await expect(viewer).toHaveAttribute("data-explode", "100");
  const full = await offsets(viewer);
  expect(Object.keys(full).sort()).toEqual(PARTS.map((p) => p.part).sort());
  // Every pair of parts is apart along the assembly direction.
  for (const a of PARTS)
    for (const b of PARTS) {
      if (a === b) continue;
      const apart = a.max + full[a.part] <= b.min + full[b.part] || b.max + full[b.part] <= a.min + full[a.part];
      expect(apart, `${a.part} and ${b.part} apart`).toBe(true);
    }
  expect(await viewer.getAttribute("data-model-rect")).not.toBe(before.rect);

  // The slider goes from assembled (0) to fully exploded (100).
  await slider.fill("50");
  await expect(viewer).toHaveAttribute("data-explode", "50");
  const half = await offsets(viewer);
  for (const p of PARTS) expect(half[p.part]).toBeCloseTo(full[p.part] / 2, 1);
  await slider.fill("0");
  await expect(viewer).toHaveAttribute("data-explode", "0");
  await slider.fill("100");
  await expect(viewer).toHaveAttribute("data-explode", "100");

  // Back to assembled: every part exactly where it was, and the same picture.
  await explode.click();
  await expect(explode).toHaveAttribute("aria-pressed", "false");
  await expect(slider).toHaveCount(0);
  await expect(viewer).toHaveAttribute("data-explode", "0");
  for (const d of Object.values(await offsets(viewer))) expect(d).toBe(0);
  expect(await viewer.getAttribute("data-model-rect")).toBe(before.rect);
  await page.mouse.move(0, 0);
  expect((await shot()).equals(before.shot), "the assembled view looks exactly as before").toBe(true);
});

test("check pins move with their parts while exploded, and take the camera to them", async ({ page, request }) => {
  const { viewer, canvas, explode } = await openCanvas(page, request);
  const before = new Map<string, { x: number; y: number }>();
  for (const r of ON_A_PART) before.set(r.id, await pinAt(canvas, r.id));

  await explode.click();
  await expect(viewer).toHaveAttribute("data-explode", "100");
  const full = await offsets(viewer);
  const moved = ON_A_PART.filter((r) => full[r.location!.part!] > 0);
  const still = ON_A_PART.filter((r) => full[r.location!.part!] === 0);
  expect(moved.length, "a check result on a part that moves").toBeGreaterThan(0);
  expect(still.length, "a check result on the part that stays").toBeGreaterThan(0);
  // A part moving up along board Z moves its pins up the screen in the default view.
  for (const r of moved) expect((await pinAt(canvas, r.id)).y, r.id).toBeLessThan(before.get(r.id)!.y - 5);
  for (const r of still) expect((await pinAt(canvas, r.id)).y, r.id).toBeCloseTo(before.get(r.id)!.y, 0);

  // Clicking a pin on a moved part centres the view on the spot where the part is drawn.
  const r = moved[0];
  await canvas.locator(`[data-testid=check-pin][data-check="${r.id}"]`).click();
  await expect(canvas.getByTestId("check-card")).toBeVisible();
  const [x, y, z] = r.location!.point;
  await expect
    .poll(async () => ((await viewer.getAttribute("data-target")) ?? "").split(",").map(Number))
    .toEqual([expect.closeTo(x, 0), expect.closeTo(y, 0), expect.closeTo(z + full[r.location!.part!], 0)]);
});

test("selecting a moved part while exploded selects it, and sends its point in the assembly", async ({ page, request }) => {
  const { viewer, canvas, chat, explode } = await openCanvas(page, request);
  await explode.click();
  await expect(viewer).toHaveAttribute("data-explode", "100");
  const full = await offsets(viewer);
  // Next to a pin on a part that moved: that part's surface, where it is drawn now. The pins sit
  // on the model (some on an edge, some under a panel), so try the spots around each such pin.
  const vb = (await viewer.boundingBox())!;
  let r: Result | undefined;
  search: for (const x of ON_A_PART.filter((c) => full[c.location!.part!] > 0)) {
    const at = await pinAt(canvas, x.id);
    for (const [dx, dy] of [[0, 16], [0, -16], [16, 0], [-16, 0]]) {
      const [px, py] = [at.x + dx, at.y + dy];
      if (px < vb.x || py < vb.y || px > vb.x + vb.width || py > vb.y + vb.height) continue;
      const hit = await page.evaluate(([cx, cy]) => document.elementFromPoint(cx, cy)?.closest("[data-testid=viewer]") !== null, [px, py]);
      if (!hit) continue;
      await page.mouse.click(px, py);
      const picked = ((await viewer.getAttribute("data-selected")) ?? "").split(",")[0];
      if (picked && partOfFace(picked)?.part === x.location!.part) {
        r = x;
        break search;
      }
    }
  }
  expect(r, "a spot next to a pin on a moved part selects that part").toBeDefined();
  await expect.poll(async () => (await viewer.getAttribute("data-selected")) ?? "").not.toBe("");
  const [faceId] = ((await viewer.getAttribute("data-selected")) ?? "").split(",");
  const part = partOfFace(faceId);
  expect(part.part).toBe(r!.location!.part);
  await expect(chat.getByTestId("selection-chip")).toBeVisible();

  const sent = page.waitForRequest((q) => q.method() === "POST" && /\/api\/sessions\/[^/]+\/messages$/.test(q.url()));
  await chat.getByRole("textbox", { name: "Message" }).fill("Why is this face here?");
  await chat.getByRole("button", { name: "Send" }).click();
  const body = (await sent).postDataJSON();
  expect(body.selection.faces).toEqual([faceId]);
  // The point is where the face is in the assembly, not where it is drawn exploded.
  const z = body.selection.point[2];
  expect(z).toBeGreaterThanOrEqual(part.min - 0.01);
  expect(z).toBeLessThanOrEqual(part.max + 0.01);
});

test("assembled and exploded match the reference screenshots", async ({ page, request }) => {
  const { canvas, viewer, explode } = await openCanvas(page, request);
  await page.mouse.move(0, 0);
  await expect(canvas).toHaveScreenshot("assembled.png", { maxDiffPixelRatio: 0.02 });
  await explode.click();
  await expect(viewer).toHaveAttribute("data-explode", "100");
  await page.mouse.move(0, 0);
  await expect(canvas).toHaveScreenshot("exploded.png", { maxDiffPixelRatio: 0.02 });
});
