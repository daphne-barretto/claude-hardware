import { readFileSync } from "node:fs";
import path from "node:path";
import { expect, test, type APIRequestContext, type Page } from "@playwright/test";
import { openExample } from "./fixtures/sessions";

// Canvas keyboard shortcuts against the mock session API. The section and comment
// shortcuts are in the keymap but have no action yet; wiring one means adding its test here.
test.skip(!!process.env.E2E_BASE_URL, "the mock API runs only with the local dev servers");

const SESSION = JSON.parse(readFileSync(path.join(__dirname, "../../fixtures/session.json"), "utf8")) as {
  prompt: string;
};

async function openCanvas(page: Page, request: APIRequestContext) {
  const session = await openExample(request);
  await request.post(`/api/sessions/${session.id}/messages`, { data: { text: SESSION.prompt } });
  await expect
    .poll(async () => (await (await request.get(`/api/sessions/${session.id}/builds`)).json()).length, {
      timeout: 30_000,
    })
    .toBeGreaterThan(0);
  await page.goto(`/session/${session.id}`);
  await expect(page.locator("[data-shell-ready=true]")).toBeAttached({ timeout: 30_000 });
  const viewer = page.getByTestId("viewer");
  await expect(viewer).toHaveAttribute("data-state", "ready", { timeout: 30_000 });
  // Start from the page, not a text field (the composer may take focus on load).
  await page.evaluate(() => (document.activeElement as HTMLElement | null)?.blur());
  return { viewer, canvas: page.getByRole("region", { name: "Canvas" }) };
}

const VIEW_KEYS: [string, string][] = [
  ["1", "front"],
  ["2", "top"],
  ["3", "right"],
  ["0", "iso"],
];

test("number keys turn the camera to front, top, right and iso", async ({ page, request }) => {
  const { viewer } = await openCanvas(page, request);
  for (const [key, view] of VIEW_KEYS) {
    await page.keyboard.press(key);
    await expect(viewer).toHaveAttribute("data-view", view);
  }
});

test("F fits the model back into view", async ({ page, request }) => {
  const { viewer } = await openCanvas(page, request);
  const box = (await viewer.boundingBox())!;
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  for (let i = 0; i < 5; i++) await page.mouse.wheel(0, -200);
  await expect(viewer).toHaveAttribute("data-fits", "false");
  await page.keyboard.press("f");
  await expect(viewer).toHaveAttribute("data-zoom", "100");
  await expect(viewer).toHaveAttribute("data-fits", "true");
});

test("? opens the sheet listing every wired shortcut; Esc closes it", async ({ page, request }) => {
  const { canvas } = await openCanvas(page, request);
  const sheet = canvas.getByRole("dialog", { name: "Keyboard shortcuts" });
  await expect(sheet).toBeHidden();
  await page.keyboard.press("?");
  await expect(sheet).toBeVisible();

  const rows = sheet.locator("[data-shortcut]");
  await expect(rows).toHaveText([
    /^Front view\s*1$/,
    /^Top view\s*2$/,
    /^Right view\s*3$/,
    /^Isometric view\s*0$/,
    /^Zoom to fit\s*F$/,
    /^Section\s*X$/,
    /^Comment\s*C$/,
    /^Hide selection\s*H$/,
    /^Isolate selection\s*I$/,
    /^Show all\s*Alt\s*H$/,
    /^Undo\s*Ctrl\s*Z$/,
    /^Redo\s*Ctrl\s*Shift\s*Z\s*Ctrl\s*Y$/,
    /^Keyboard shortcuts\s*\?$/,
  ]);

  await page.keyboard.press("Escape");
  await expect(sheet).toBeHidden();
  await page.keyboard.press("?");
  await expect(sheet).toBeVisible();
  await sheet.getByRole("button", { name: "Close keyboard shortcuts" }).click();
  await expect(sheet).toBeHidden();
});

test("the shortcuts on the sheet work while it is open", async ({ page, request }) => {
  const { viewer, canvas } = await openCanvas(page, request);
  await page.keyboard.press("?");
  await expect(canvas.getByRole("dialog", { name: "Keyboard shortcuts" })).toBeVisible();
  await page.keyboard.press("2");
  await expect(viewer).toHaveAttribute("data-view", "top");
});

test("no shortcut fires while typing in the chat", async ({ page, request }) => {
  const { viewer, canvas } = await openCanvas(page, request);
  await page.keyboard.press("2");
  await expect(viewer).toHaveAttribute("data-view", "top");

  const message = page.getByRole("region", { name: "Chat" }).getByRole("textbox", { name: "Message" });
  await message.click();
  await page.keyboard.type("1 0 3 f ?");
  await expect(message).toHaveValue("1 0 3 f ?");
  await expect(viewer).toHaveAttribute("data-view", "top");
  await expect(canvas.getByRole("dialog", { name: "Keyboard shortcuts" })).toBeHidden();
});

test("no shortcut fires while a text field has focus", async ({ page, request }) => {
  const { viewer, canvas } = await openCanvas(page, request);
  await page.keyboard.press("3");
  await expect(viewer).toHaveAttribute("data-view", "right");

  await page.keyboard.press("ControlOrMeta+k");
  const search = page.getByRole("dialog", { name: "Search sessions" }).getByRole("textbox");
  await expect(search).toBeFocused();
  await page.keyboard.type("10f?");
  await expect(search).toHaveValue("10f?");
  await expect(viewer).toHaveAttribute("data-view", "right");
  await expect(canvas.getByRole("dialog", { name: "Keyboard shortcuts" })).toBeHidden();

  // A text field the page adds (for example a future panel's input) is covered too.
  await page.keyboard.press("Escape");
  await page.evaluate(() => {
    const input = document.createElement("input");
    input.setAttribute("aria-label", "Sample field");
    document.body.append(input);
    input.focus();
  });
  await page.keyboard.type("0");
  await expect(viewer).toHaveAttribute("data-view", "right");
});

// The fixture mesh's parts and faces, by the mesh contract (contracts/README.md, "Mesh").
const MESH = (
  JSON.parse(readFileSync(path.join(__dirname, "../../fixtures/enclosure.gltf"), "utf8")) as {
    nodes: { extras?: { part?: string; faces?: { id: string }[] } }[];
  }
).nodes.flatMap((n) => (n.extras?.part ? [{ part: n.extras.part, faces: n.extras.faces ?? [] }] : []));

test("H hides the selected parts, I isolates them and Alt+H shows all", async ({ page, request }) => {
  const { viewer, canvas } = await openCanvas(page, request);
  const panel = canvas.getByRole("region", { name: "Parts" });
  const parts = MESH.map((m) => m.part);
  expect(parts.length).toBeGreaterThan(1);
  const hide = (p: string) => panel.getByRole("button", { name: `Hide: ${p}` });
  const isolateBtn = (p: string) => panel.getByRole("button", { name: `Isolate: ${p}` });
  const shortcut = async (key: string) => {
    await canvas.getByRole("button", { name: "Home view" }).focus();
    await page.keyboard.press(key);
  };

  // Nothing selected: H and I change nothing.
  await shortcut("h");
  await shortcut("i");
  await expect(viewer).toHaveAttribute("data-hidden", "");

  // A part selected: H hides it.
  const [first] = parts;
  await panel.getByRole("button", { name: `Select ${first}` }).click();
  await shortcut("h");
  await expect(viewer).toHaveAttribute("data-hidden", first);
  for (const p of parts) await expect(hide(p)).toHaveAttribute("aria-pressed", String(p === first));

  await shortcut("Alt+h");
  await expect(viewer).toHaveAttribute("data-hidden", "");
  for (const p of parts) await expect(hide(p)).toHaveAttribute("aria-pressed", "false");

  // Another part selected: I isolates it.
  const target = MESH.find((m) => m.part !== first && m.faces.length) ?? MESH.find((m) => m.faces.length)!;
  await panel.getByRole("button", { name: `Select ${target.part}` }).click();
  await expect(viewer).toHaveAttribute("data-selected", target.part);
  await shortcut("i");
  // Isolating hides the board revision's parts too.
  const boardParts = ((await viewer.getAttribute("data-board-parts")) ?? "").split(",").filter(Boolean);
  expect(boardParts.length).toBeGreaterThan(0);
  await expect(viewer).toHaveAttribute("data-hidden", [...parts, ...boardParts].filter((p) => p !== target.part).join(","));
  await expect(isolateBtn(target.part)).toHaveAttribute("aria-pressed", "true");
  for (const p of parts) await expect(hide(p)).toHaveAttribute("aria-pressed", String(p !== target.part));

  await shortcut("Alt+h");
  await expect(viewer).toHaveAttribute("data-hidden", "");
  await expect(isolateBtn(target.part)).toHaveAttribute("aria-pressed", "false");
  for (const p of parts) await expect(hide(p)).toHaveAttribute("aria-pressed", "false");
});
