import { readFileSync } from "node:fs";
import path from "node:path";
import { expect, test, type APIRequestContext, type Locator, type Page } from "@playwright/test";
import { BOARD_PART } from "../src/components/canvas/boardParts";
import { componentBody, bodyCenter, type Board } from "../src/components/canvas/inspect";
import { indexFromGltf, resolveId } from "../src/components/canvas/selection";
import { answerOpenQuestion } from "./inventory/ui-inventory";

// The build's board revision as parts against the mock session API (docs/architecture.md).
// The board, its components and the enclosure parts come from demos/b-parasite/ and fixtures/, never from here.
test.skip(!!process.env.E2E_BASE_URL, "the mock API runs only with the local dev servers");

const root = path.join(__dirname, "../..");
const read = (file: string) => JSON.parse(readFileSync(path.join(root, file), "utf8"));
const SESSION = read("fixtures/session.json") as { prompt: string };
const BUILD = read("fixtures/build.json") as { board: { name: string; revision: string } };
const BOARD = read(`demos/b-parasite/${BUILD.board.name}-${BUILD.board.revision}.json`) as Board;
const INDEX = indexFromGltf(read("fixtures/enclosure.gltf"));
const PARTS = INDEX.map((p) => p.part);
const REFS = BOARD.components.map((c) => c.ref);

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
  await expect(viewer).toHaveAttribute("data-board-parts", [BOARD_PART, ...REFS].join(","));
  return {
    viewer,
    chat: page.getByRole("region", { name: "Chat" }),
    parts: page.getByRole("region", { name: "Parts" }),
  };
}

/** Where a board point is in the page, from the viewer's board-to-clip matrix. */
async function toPage(viewer: Locator, [x, y, z]: number[]): Promise<[number, number]> {
  const m = ((await viewer.getAttribute("data-board-to-clip")) ?? "").split(",").map(Number);
  const w = m[3] * x + m[7] * y + m[11] * z + m[15];
  const cx = (m[0] * x + m[4] * y + m[8] * z + m[12]) / w;
  const cy = (m[1] * x + m[5] * y + m[9] * z + m[13]) / w;
  const box = (await viewer.boundingBox())!;
  return [box.x + ((cx + 1) / 2) * box.width, box.y + ((1 - cy) / 2) * box.height];
}

test("the board and its components are listed after the enclosure's parts", async ({ page, request }) => {
  const { parts } = await openCanvas(page, request);
  const list = parts.getByRole("list", { name: "Part list" });
  const rows = await list.locator(":scope > li[data-part]").evaluateAll((els) => els.map((e) => e.getAttribute("data-part")));
  expect(rows.slice(0, PARTS.length)).toEqual(PARTS);
  expect(rows).toContain(BOARD_PART);

  // The components open under the board, closed at first, named by reference designator.
  await expect(parts.getByRole("list", { name: "Components" })).toHaveCount(0);
  await parts.getByRole("button", { name: `Components ${REFS.length}` }).click();
  const components = parts.getByRole("list", { name: "Components" });
  for (const ref of REFS) await expect(components.getByRole("button", { name: `Select ${ref}`, exact: true })).toBeVisible();
});

test("hiding the enclosure shows the board; a component hides and isolates like a part", async ({ page, request }) => {
  const { viewer, parts } = await openCanvas(page, request);
  // The middle of the canvas, clear of the panels that sit over it.
  const v = (await viewer.boundingBox())!;
  const clip = { x: v.x + 280, y: v.y + 80, width: v.width - 560, height: v.height - 200 };
  const gl = { screenshot: () => page.screenshot({ clip, animations: "disabled" }) };
  await parts.getByRole("button", { name: `Components ${REFS.length}` }).click();
  const hide = (p: string) => parts.getByRole("button", { name: `Hide: ${p}`, exact: true });

  // Closed up, the board is not drawn: hiding it changes nothing on screen.
  const closed = await gl.screenshot();
  await hide(BOARD_PART).click();
  await expect(viewer).toHaveAttribute("data-hidden", BOARD_PART);
  await expect.poll(async () => (await gl.screenshot()).equals(closed)).toBe(true);
  await hide(BOARD_PART).click();

  // Every listed part hidden: an empty canvas.
  for (const p of [...PARTS, BOARD_PART, ...REFS]) await hide(p).click();
  await expect(viewer).toHaveAttribute("data-hidden", [...PARTS, BOARD_PART, ...REFS].join(","));
  const empty = await gl.screenshot();

  // The enclosure hidden, the board and its components shown: they are drawn.
  for (const p of [BOARD_PART, ...REFS]) await hide(p).click();
  await expect(viewer).toHaveAttribute("data-hidden", PARTS.join(","));
  await expect.poll(async () => (await gl.screenshot()).equals(empty)).toBe(false);
  const board = await gl.screenshot();

  // Hiding one component changes the drawing.
  const top = BOARD.components.find((c) => c.side === "top")!;
  await hide(top.ref).click();
  await expect.poll(async () => (await gl.screenshot()).equals(board)).toBe(false);
  await hide(top.ref).click();

  // Isolating a component hides every other part, the board and the enclosure included.
  const isolate = parts.getByRole("button", { name: `Isolate: ${top.ref}`, exact: true });
  await isolate.click();
  await expect(isolate).toHaveAttribute("aria-pressed", "true");
  await expect(viewer).toHaveAttribute("data-hidden", [...PARTS, BOARD_PART, ...REFS].filter((p) => p !== top.ref).join(","));
  await expect.poll(async () => (await gl.screenshot()).equals(empty)).toBe(false);

  await parts.getByRole("button", { name: "Show all", exact: true }).click();
  await expect(viewer).toHaveAttribute("data-hidden", "");
});

test("a drawn component is picked whole by a click, and shows its reference designator", async ({ page, request }) => {
  const { viewer, chat, parts } = await openCanvas(page, request);
  await parts.getByRole("button", { name: `Isolate: ${BOARD_PART}`, exact: true }).click();
  // Isolating the board hides its components: show the one to click again.
  await parts.getByRole("button", { name: `Components ${REFS.length}` }).click();
  // The largest top component, so its centre is easy to hit.
  const area = (c: (typeof BOARD.components)[number]) => c.size[0] * c.size[1];
  const target = BOARD.components.filter((c) => c.side === "top").reduce((a, b) => (area(b) > area(a) ? b : a));
  await parts.getByRole("button", { name: `Hide: ${target.ref}`, exact: true }).click();
  await expect(viewer).toHaveAttribute("data-animating", "false");

  const body = componentBody(BOARD, target);
  const [x, y] = await toPage(viewer, [...bodyCenter(body).slice(0, 2), body.z1]);
  await page.mouse.click(x, y);
  await expect(viewer).toHaveAttribute("data-selected", target.ref);
  await expect(parts.getByRole("button", { name: `Select ${target.ref}`, exact: true })).toHaveAttribute("aria-pressed", "true");
  await expect(chat.getByTestId("selection-chip")).toContainText(target.ref);
});

test("select by ID finds the board and its components after the enclosure's IDs", async ({ page, request }) => {
  const { viewer, parts } = await openCanvas(page, request);
  const box = parts.getByRole("textbox", { name: "Select by ID" });
  for (const id of [BOARD_PART, ...REFS]) {
    // An ID the enclosure knows (an opening named after a component) still selects that first.
    const enclosure = resolveId(INDEX, id).map((p) => p.id);
    await box.fill(id.toLowerCase());
    await box.press("Enter");
    await expect(viewer).toHaveAttribute("data-selected", (enclosure.length ? enclosure : [id]).join(","));
  }
});
