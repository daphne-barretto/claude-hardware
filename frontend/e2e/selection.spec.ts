import { readFileSync } from "node:fs";
import path from "node:path";
import { expect, test, type APIRequestContext, type Locator, type Page } from "@playwright/test";
import { answerOpenQuestion } from "./inventory/ui-inventory";
import { openExample } from "./fixtures/sessions";

// Selection, hide and isolate against the mock session API (docs/architecture.md).
// Part, face and feature names come from the mesh fixture, never from here.
test.skip(!!process.env.E2E_BASE_URL, "the mock API runs only with the local dev servers");

type Extras = { part: string; faces: { id: string; feature: string }[]; edges: { id: string; feature: string }[] };
const read = (name: string) => JSON.parse(readFileSync(path.join(__dirname, "../../fixtures", name), "utf8"));
const MESH = read("enclosure.gltf") as { nodes: { extras?: Extras }[] };
const SESSION = read("session.json") as { prompt: string };
const PARTS = MESH.nodes.flatMap((n) => (n.extras?.part ? [n.extras] : []));
const FACES = PARTS.flatMap((p) => p.faces.map((f) => ({ ...f, part: p.part })));
const EDGES = PARTS.flatMap((p) => p.edges.map((e) => ({ ...e, part: p.part })));
const faceById = (id: string) => FACES.find((f) => f.id === id)!;
// A board-derived feature with several faces, such as the opening for a component.
const COMPONENT_FEATURE = FACES.find(
  (f) => f.feature !== f.part && FACES.filter((g) => g.feature === f.feature).length > 1,
)!.feature;

async function openCanvas(page: Page, request: APIRequestContext) {
  const session = await openExample(request);
  await request.post(`/api/sessions/${session.id}/messages`, { data: { text: SESSION.prompt } });
  await expect
    .poll(async () => (await (await request.get(`/api/sessions/${session.id}/builds`)).json()).length, { timeout: 30_000 })
    .toBeGreaterThan(0);
  // The session API takes only the answer while the run waits on its question.
  await expect
    .poll(async () => (await (await request.get(`/api/sessions/${session.id}`)).json()).status, { timeout: 30_000 })
    .not.toBe("running");
  await answerOpenQuestion(page, `/api/sessions/${session.id}`);
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto(`/session/${session.id}`);
  await expect(page.locator("[data-shell-ready=true]")).toBeAttached({ timeout: 30_000 });
  const viewer = page.getByTestId("viewer");
  await expect(viewer).toHaveAttribute("data-state", "ready", { timeout: 30_000 });
  return {
    viewer,
    canvas: page.getByRole("region", { name: "Canvas" }),
    chat: page.getByRole("region", { name: "Chat" }),
    parts: page.getByRole("region", { name: "Parts" }),
  };
}

const selected = async (viewer: Locator) => ((await viewer.getAttribute("data-selected")) ?? "").split(",").filter(Boolean);

/** Clicks the middle of the model (a click, not a drag), returning the picked IDs. */
async function clickModel(page: Page, viewer: Locator, modifiers: ("Shift")[] = []) {
  const box = (await viewer.boundingBox())!;
  await page.mouse.click(box.x + box.width / 2, box.y + box.height / 2, { modifiers } as never);
  if (modifiers.includes("Shift")) await page.keyboard.up("Shift");
  return selected(viewer);
}

test("selecting a face shows its feature on a chip and sends its ID with the next message", async ({ page, request }) => {
  const { viewer, chat } = await openCanvas(page, request);
  await expect(viewer).toHaveAttribute("data-pick", "face");

  const box = (await viewer.boundingBox())!;
  await page.mouse.click(box.x + box.width / 2, box.y + box.height / 2);
  await expect.poll(() => selected(viewer)).toHaveLength(1);
  const [faceId] = await selected(viewer);
  const face = faceById(faceId);
  expect(face, `${faceId} is a face of the fixture mesh`).toBeDefined();

  const chip = chat.getByTestId("selection-chip");
  await expect(chip).toHaveText(face.feature);
  await expect(chip).toHaveAttribute("title", new RegExp(faceId.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")));

  const sent = page.waitForRequest((r) => r.method() === "POST" && /\/api\/sessions\/[^/]+\/messages$/.test(r.url()));
  await chat.getByRole("textbox", { name: "Message" }).fill("Why is this face here?");
  await chat.getByRole("button", { name: "Send" }).click();
  const body = (await sent).postDataJSON();
  expect(body.selection.faces).toEqual([faceId]);
  expect(body.selection.part).toBe(face.part);
  expect(body.selection.point).toHaveLength(3);

  // The selection went with that message: the chip is on the message, and the canvas is clear.
  await expect(chat.getByTestId("selection-chip")).toHaveCount(1);
  await expect(chat.getByRole("button", { name: "Clear selection" })).toHaveCount(0);
  await expect.poll(() => selected(viewer)).toEqual([]);
});

test("the selection is highlighted; clicking empty space or Escape clears it", async ({ page, request }) => {
  const { viewer, chat, canvas } = await openCanvas(page, request);
  const gl = canvas.locator("canvas");
  const before = await gl.screenshot();
  await clickModel(page, viewer);
  await expect.poll(() => selected(viewer)).toHaveLength(1);
  await expect.poll(async () => (await gl.screenshot()).equals(before)).toBe(false);

  await page.keyboard.press("Escape");
  await expect.poll(() => selected(viewer)).toEqual([]);
  await expect(chat.getByTestId("selection-chip")).toHaveCount(0);

  await clickModel(page, viewer);
  await expect.poll(() => selected(viewer)).toHaveLength(1);
  const box = (await viewer.boundingBox())!;
  // Empty space: the right edge, below the view cube and above the inspection tools.
  await page.mouse.click(box.x + box.width - 20, box.y + box.height * 0.6);
  await expect.poll(() => selected(viewer)).toEqual([]);
});

test("part mode picks the whole part; select by ID finds edges and board components", async ({ page, request }) => {
  const { viewer, chat, parts } = await openCanvas(page, request);
  const chip = chat.getByTestId("selection-chip");

  await parts.getByRole("radiogroup", { name: "Select" }).getByRole("radio", { name: "Part" }).click();
  await expect(viewer).toHaveAttribute("data-pick", "part");
  await clickModel(page, viewer);
  await expect.poll(() => selected(viewer)).toHaveLength(1);
  const [part] = await selected(viewer);
  expect(PARTS.map((p) => p.part)).toContain(part);
  await expect(chip).toHaveText(part);

  const byId = parts.getByRole("textbox", { name: "Select by ID" });
  await byId.fill(EDGES[0].id);
  await byId.press("Enter");
  await expect.poll(() => selected(viewer)).toEqual([EDGES[0].id]);
  await expect(chip).toHaveText(EDGES[0].feature);

  // The component reference, e.g. Q4 for opening_Q4, selects the faces of its feature.
  const ref = COMPONENT_FEATURE.slice(COMPONENT_FEATURE.lastIndexOf("_") + 1);
  await byId.fill(ref);
  await byId.press("Enter");
  const faces = FACES.filter((f) => f.feature === COMPONENT_FEATURE).map((f) => f.id);
  await expect.poll(() => selected(viewer)).toEqual(faces);
  await expect(chip.getByTestId("selection-label")).toHaveText(`${COMPONENT_FEATURE} · ${faces.length} faces`);
  await expect(chip.getByRole("listitem")).toHaveText(faces);

  await byId.fill("no-such-id");
  await byId.press("Enter");
  await expect(parts.getByText("No part, face, edge, or component with that ID.")).toBeVisible();

  await parts.getByRole("button", { name: `Select ${PARTS[0].part}` }).click();
  await expect(chip).toHaveText(PARTS[0].part);
});

test("hide, isolate and transparency work per part", async ({ page, request }) => {
  const { viewer, parts, canvas } = await openCanvas(page, request);
  const gl = canvas.locator("canvas");
  const names = PARTS.map((p) => p.part);
  // The board revision's parts are listed too: isolating hides them as well.
  await expect(viewer).toHaveAttribute("data-board-parts", /./);
  const listed = [...names, ...((await viewer.getAttribute("data-board-parts")) ?? "").split(",")];

  // Pick the part in the middle of the view, then hide it: it no longer draws or picks.
  await clickModel(page, viewer);
  await expect.poll(() => selected(viewer)).toHaveLength(1);
  const front = faceById((await selected(viewer))[0]).part;
  await page.keyboard.press("Escape");

  for (const part of names) {
    const shown = await gl.screenshot();
    const hide = parts.getByRole("button", { name: `Hide: ${part}` });
    await hide.click();
    await expect(hide).toHaveAttribute("aria-pressed", "true");
    await expect(viewer).toHaveAttribute("data-hidden", part);
    await expect.poll(async () => (await gl.screenshot()).equals(shown)).toBe(false);
    if (part === front) {
      const picked = await clickModel(page, viewer);
      expect(picked.map((id) => faceById(id).part)).not.toContain(part);
      await page.keyboard.press("Escape");
    }
    await hide.click();
    await expect(hide).toHaveAttribute("aria-pressed", "false");
    await expect(viewer).toHaveAttribute("data-hidden", "");
  }

  // Isolate shows only that part; isolating it again shows all of them.
  for (const part of names) {
    const isolate = parts.getByRole("button", { name: `Isolate: ${part}` });
    await isolate.click();
    await expect(isolate).toHaveAttribute("aria-pressed", "true");
    await expect(viewer).toHaveAttribute("data-hidden", listed.filter((p) => p !== part).join(","));
    await isolate.click();
    await expect(viewer).toHaveAttribute("data-hidden", "");
  }
  await parts.getByRole("button", { name: `Isolate: ${names[0]}` }).click();
  await parts.getByRole("button", { name: "Show all", exact: true }).click();
  await expect(viewer).toHaveAttribute("data-hidden", "");

  // Transparency is per part and changes the drawing.
  for (const part of names) {
    const opaque = await gl.screenshot();
    const transparent = parts.getByRole("button", { name: `Transparent: ${part}` });
    await transparent.click();
    await expect(transparent).toHaveAttribute("aria-pressed", "true");
    await expect(viewer).toHaveAttribute("data-transparent", part);
    await expect.poll(async () => (await gl.screenshot()).equals(opaque)).toBe(false);
    await transparent.click();
    await expect(viewer).toHaveAttribute("data-transparent", "");
  }
});

// --- Selection filters and multi-select --------------------------------------------

// Board components with an opening in the mesh: features follow `opening_<ref>[_<kind>]`.
const componentOf = (feature: string) => /^opening_([^_]+)/.exec(feature)?.[1];
const REFS = [...new Set(FACES.flatMap((f) => componentOf(f.feature) ?? []))];
const ALL_PARTS = PARTS.map((p) => p.part);

const filter = (parts: Locator, name: string) =>
  parts.getByRole("radiogroup", { name: "Select" }).getByRole("radio", { name, exact: true });

/** The model's on-screen box in page pixels, from the viewer's data-model-rect. */
async function modelBox(viewer: Locator) {
  const v = (await viewer.boundingBox())!;
  const [x, y, w, h] = ((await viewer.getAttribute("data-model-rect")) ?? "").split(",").map(Number);
  return { x: v.x + x, y: v.y + y, width: w, height: h };
}

/** Drags a box with the left button from corner to corner, holding `modifier`. */
async function dragBox(page: Page, from: [number, number], to: [number, number], modifier?: "Shift" | "Meta") {
  if (modifier) await page.keyboard.down(modifier);
  await page.mouse.move(...from);
  await page.mouse.down();
  await page.mouse.move((from[0] + to[0]) / 2, (from[1] + to[1]) / 2, { steps: 4 });
  await expect(page.getByTestId("select-box")).toBeVisible();
  await page.mouse.move(...to, { steps: 4 });
  await page.mouse.up();
  if (modifier) await page.keyboard.up(modifier);
  await expect(page.getByTestId("select-box")).toHaveCount(0);
}

/**
 * Boxes the whole model (with a margin), holding `modifier`. The drag starts at the bottom right,
 * clear of the parts panel; once it starts the viewer keeps the pointer, so the box can end
 * behind the panel.
 */
async function boxModel(page: Page, viewer: Locator, modifier?: "Shift" | "Meta") {
  const m = await modelBox(viewer);
  await dragBox(page, [m.x + m.width + 8, m.y + m.height + 8], [m.x - 8, m.y - 8], modifier);
}

/**
 * A page point on what is highlighted now: the pixels that differ from `before`, taking one
 * whose neighbours all differ too (so not on an outline) and that the parts panel doesn't cover.
 */
async function highlightedPoint(page: Page, gl: Locator, before: Buffer, panel: Locator): Promise<[number, number]> {
  const after = await gl.screenshot();
  const box = (await gl.boundingBox())!;
  const pb = (await panel.boundingBox())!;
  // The panel in screenshot pixels (the screenshot is the canvas at device scale).
  const scale = (await page.evaluate(() => window.devicePixelRatio)) || 1;
  const covered = [(pb.x - box.x) * scale, (pb.y - box.y) * scale, (pb.x + pb.width - box.x) * scale, (pb.y + pb.height - box.y) * scale];
  const hit = await page.evaluate(
    async ([a, b, c]) => {
      const pixels = async (b64: string) => {
        const img = await createImageBitmap(await (await fetch(`data:image/png;base64,${b64}`)).blob());
        const c = new OffscreenCanvas(img.width, img.height);
        const ctx = c.getContext("2d")!;
        ctx.drawImage(img, 0, 0);
        return ctx.getImageData(0, 0, img.width, img.height);
      };
      const [p, q] = [await pixels(a), await pixels(b)];
      const { width: w, height: h } = p;
      const differs = (x: number, y: number) => {
        const i = (y * w + x) * 4;
        return Math.abs(p.data[i] - q.data[i]) + Math.abs(p.data[i + 1] - q.data[i + 1]) + Math.abs(p.data[i + 2] - q.data[i + 2]) > 30;
      };
      const found: [number, number][] = [];
      for (let y = 2; y < h - 2; y++)
        for (let x = 2; x < w - 2; x++) {
          if (x >= c[0] && x <= c[2] && y >= c[1] && y <= c[3]) continue;
          if ([-2, 0, 2].every((dy) => [-2, 0, 2].every((dx) => differs(x + dx, y + dy)))) found.push([x, y]);
        }
      if (!found.length) return null;
      const [x, y] = found[Math.floor(found.length / 2)];
      return [x, y, w, h];
    },
    [before.toString("base64"), after.toString("base64"), covered] as const,
  );
  expect(hit, "the highlight is visible").not.toBeNull();
  const [x, y, w, h] = hit!;
  // Screenshot pixels to page pixels.
  return [box.x + ((x + 0.5) * box.width) / w, box.y + ((y + 0.5) * box.height) / h];
}

test("the filter toolbar limits a click to one kind, board components included", async ({ page, request }) => {
  const { viewer, chat, parts, canvas } = await openCanvas(page, request);
  const gl = canvas.locator("canvas");
  const chip = chat.getByTestId("selection-chip");
  expect(REFS.length, "the mesh has openings for board components").toBeGreaterThan(0);
  const radios = parts.getByRole("radiogroup", { name: "Select" }).getByRole("radio");
  await expect(radios).toHaveText(["Face", "Edge", "Part", "Component"]);

  // Where a component's opening is on screen: highlight it by reference and look.
  const ref = REFS[0];
  const before = await gl.screenshot();
  const byId = parts.getByRole("textbox", { name: "Select by ID" });
  await byId.fill(ref);
  await byId.press("Enter");
  const at = await highlightedPoint(page, gl, before, parts);
  await chat.getByRole("button", { name: "Clear selection" }).click();
  await expect.poll(() => selected(viewer)).toEqual([]);

  // Face: the opening's face. Part: its part. Component: the component's reference.
  await page.mouse.click(...at);
  await expect.poll(() => selected(viewer)).toHaveLength(1);
  const face = faceById((await selected(viewer))[0]);
  expect(componentOf(face.feature)).toBe(ref);

  await filter(parts, "Part").click();
  await page.mouse.click(...at);
  await expect.poll(() => selected(viewer)).toEqual([face.part]);
  await expect(chip).toHaveText(face.part);

  await filter(parts, "Component").click();
  await expect(viewer).toHaveAttribute("data-pick", "component");
  await page.mouse.click(...at);
  await expect.poll(() => selected(viewer)).toEqual([ref]);
  await expect(chip).toHaveText(ref);

  // A face that is no component's opening picks nothing in the component filter.
  await page.keyboard.press("Escape");
  await filter(parts, "Face").click();
  await clickModel(page, viewer);
  await expect.poll(() => selected(viewer)).toHaveLength(1);
  const plain = faceById((await selected(viewer))[0]);
  expect(componentOf(plain.feature), "the middle of the model is not an opening").toBeUndefined();
  await page.keyboard.press("Escape");
  await filter(parts, "Component").click();
  await clickModel(page, viewer);
  await expect.poll(() => selected(viewer)).toEqual([]);
  await expect(chip).toHaveCount(0);
});

test("Shift- and Cmd-click add and remove", async ({ page, request }) => {
  const { viewer, chat } = await openCanvas(page, request);
  const m = await modelBox(viewer);
  // Two points on the model with different faces.
  const first: [number, number] = [m.x + m.width / 2, m.y + m.height / 2];
  await page.mouse.click(...first);
  await expect.poll(() => selected(viewer)).toHaveLength(1);
  const [a] = await selected(viewer);
  let second: [number, number] | null = null;
  for (const [fx, fy] of [[0.3, 0.5], [0.7, 0.5], [0.5, 0.3], [0.5, 0.7], [0.35, 0.35], [0.65, 0.65]]) {
    const p: [number, number] = [m.x + m.width * fx, m.y + m.height * fy];
    await page.mouse.click(...p);
    const now = await selected(viewer);
    if (now.length === 1 && now[0] !== a) {
      second = p;
      break;
    }
  }
  expect(second, "a second face to click").not.toBeNull();
  const [b] = await selected(viewer);

  for (const modifier of ["Shift", "Meta", "Control"] as const) {
    await page.mouse.click(...first);
    await expect.poll(() => selected(viewer)).toEqual([a]);
    await page.keyboard.down(modifier);
    await page.mouse.click(...second!);
    await expect.poll(() => selected(viewer), `${modifier}-click adds`).toEqual([a, b]);
    await page.mouse.click(...first);
    await expect.poll(() => selected(viewer), `${modifier}-click removes`).toEqual([b]);
    await page.keyboard.up(modifier);
  }
  await expect(chat.getByTestId("selection-chip")).toHaveText(faceById(b).feature);
});

test("box select picks everything of the filter's kind inside the box", async ({ page, request }) => {
  const { viewer, parts } = await openCanvas(page, request);
  const boxToggle = parts.getByRole("button", { name: "Box select" });
  const view = await viewer.getAttribute("data-view");
  await boxToggle.click();
  await expect(boxToggle).toHaveAttribute("aria-pressed", "true");
  await expect(viewer).toHaveAttribute("data-box", "true");

  // The whole model: every face (dragging draws a box and does not orbit).
  await boxModel(page, viewer);
  await expect.poll(async () => (await selected(viewer)).sort()).toEqual(FACES.map((f) => f.id).sort());
  await expect(viewer).toHaveAttribute("data-view", view!);

  // A box over part of the model takes some faces, not all.
  const m = await modelBox(viewer);
  await dragBox(page, [m.x + m.width * 0.4, m.y + m.height * 0.4], [m.x + m.width * 0.6, m.y + m.height * 0.6]);
  const some = await selected(viewer);
  expect(some.length).toBeGreaterThan(0);
  expect(some.length).toBeLessThan(FACES.length);

  // Hidden parts are left out.
  await parts.getByRole("button", { name: `Hide: ${ALL_PARTS[0]}` }).click();
  await boxModel(page, viewer);
  await expect
    .poll(async () => (await selected(viewer)).sort())
    .toEqual(FACES.filter((f) => f.part !== ALL_PARTS[0]).map((f) => f.id).sort());
  await parts.getByRole("button", { name: `Hide: ${ALL_PARTS[0]}` }).click();

  // Edges, parts and components.
  await filter(parts, "Edge").click();
  await boxModel(page, viewer);
  await expect.poll(async () => (await selected(viewer)).sort()).toEqual(EDGES.map((e) => e.id).sort());
  await filter(parts, "Part").click();
  await boxModel(page, viewer);
  await expect.poll(async () => (await selected(viewer)).sort()).toEqual([...ALL_PARTS].sort());
  await filter(parts, "Component").click();
  await boxModel(page, viewer);
  await expect.poll(async () => (await selected(viewer)).sort()).toEqual([...REFS].sort());

  // Off again: a drag orbits.
  await boxToggle.click();
  await expect(boxToggle).toHaveAttribute("aria-pressed", "false");
  const c = await modelBox(viewer);
  await page.mouse.move(c.x + c.width / 2, c.y + c.height / 2);
  await page.mouse.down();
  await page.mouse.move(c.x + c.width / 2 + 120, c.y + c.height / 2, { steps: 6 });
  await page.mouse.up();
  await expect(viewer).not.toHaveAttribute("data-view", view!);
});

test("a multi-selection is one chip listing every item, and the agent receives every ID", async ({ page, request }) => {
  const { viewer, chat, parts } = await openCanvas(page, request);
  await parts.getByRole("button", { name: "Box select" }).click();
  await filter(parts, "Part").click();
  await boxModel(page, viewer);
  await filter(parts, "Component").click();
  await boxModel(page, viewer, "Shift");
  // A box drawn with Shift or Cmd held adds to the selection.
  await filter(parts, "Edge").click();
  await boxModel(page, viewer, "Meta");
  const ids = [...ALL_PARTS, ...REFS, ...EDGES.map((e) => e.id)];
  await expect.poll(async () => (await selected(viewer)).sort()).toEqual([...ids].sort());

  const chips = chat.getByTestId("selection-chip");
  await expect(chips).toHaveCount(1);
  await expect(chips.getByTestId("selection-label")).toHaveText(
    `${ALL_PARTS.length} parts, ${REFS.length} component${REFS.length > 1 ? "s" : ""}, ${EDGES.length} edges`,
  );
  const items = chips.getByRole("list", { name: "Selected items" }).getByRole("listitem");
  await expect(items).toHaveCount(ids.length);
  expect((await items.allTextContents()).sort()).toEqual([...ids].sort());

  const sent = page.waitForRequest((r) => r.method() === "POST" && /\/api\/sessions\/[^/]+\/messages$/.test(r.url()));
  await chat.getByRole("textbox", { name: "Message" }).fill("Check these together");
  await chat.getByRole("button", { name: "Send" }).click();
  const { selection } = (await sent).postDataJSON();
  expect([...selection.parts].sort()).toEqual([...ALL_PARTS].sort());
  expect([...selection.components].sort()).toEqual([...REFS].sort());
  expect([...selection.edges].sort()).toEqual(EDGES.map((e) => e.id).sort());
  await expect.poll(() => selected(viewer)).toEqual([]);
});
