import { readFileSync } from "node:fs";
import path from "node:path";
import { expect, test, type APIRequestContext, type Page } from "@playwright/test";
import { openExample } from "./fixtures/sessions";
import { answerOpenQuestion } from "./inventory/ui-inventory";
import { sketchTargets } from "./sketchFaces";

// Undo and redo are one timeline of design changes, whoever made them (the design timeline,
// `GET .../history`): a hand edit and then a Claude run are undone newest first and redone in
// order. And a build that fails is shown plainly over the last good model, which comments go to.
// Faces come from the fixture mesh (e2e/sketchFaces.ts); the scripted Claude's "Move the
// openings" run from backend/app/e2e_api.py; the mock's "Break the build" run from
// backend/app/mock_api.py.
test.skip(!!process.env.E2E_BASE_URL, "the mock and scripted backends run only with the local dev servers");

type Step = { text: string; actor: "user" | "claude" };
type History = { steps: Step[]; position: number };
type Build = { id: string; status: string; failure?: string | null };

const { flat } = sketchTargets();
const MOVE_OPENINGS = "Move the openings";
const SESSION = JSON.parse(readFileSync(path.resolve(__dirname, "../../fixtures/session.json"), "utf8")) as { prompt: string };
const shot = (page: Page, name: string) =>
  page.screenshot({ path: path.resolve(__dirname, `../test-results/screenshots/modeling-tools/${name}.png`) });

const get = async <T>(request: APIRequestContext, url: string) => {
  const res = await request.get(url);
  expect(res.ok(), `${url}: ${res.status()}`).toBe(true);
  return (await res.json()) as T;
};

async function open(page: Page, id: string) {
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto(`/session/${id}`);
  await expect(page.locator("[data-shell-ready=true]")).toBeAttached({ timeout: 30_000 });
  const viewer = page.getByTestId("viewer");
  await expect(viewer).toHaveAttribute("data-state", "ready", { timeout: 30_000 });
  const canvas = page.getByRole("region", { name: "Canvas" });
  return { viewer, canvas, toolbar: canvas.getByRole("toolbar", { name: "Modeling tools" }), chat: page.getByRole("region", { name: "Chat" }) };
}

test.describe("on the real backend with a scripted Claude", () => {
  test.beforeEach(async ({ context, baseURL }) => {
    await context.addCookies([{ name: "e2e-backend", value: "live", url: baseURL! }]);
  });

  test("a hand edit, then Claude's run: Undo undoes Claude's, then mine; Redo brings them back", async ({ page }) => {
    test.setTimeout(180_000);
    const { request } = page;
    const session = await openExample(request);
    const api = `/api/sessions/${session.id}`;
    await expect.poll(async () => (await get<Build[]>(request, `${api}/builds`)).length, { timeout: 60_000 }).toBeGreaterThan(0);
    const { canvas, toolbar, chat } = await open(page, session.id);
    const history = () => get<History>(request, `${api}/history`);
    const edits = async () => (await get<{ tag: string }[]>(request, `${api}/edits`)).map((e) => e.tag);

    // By hand: push a flat face out 1 mm.
    await toolbar.getByRole("button", { name: "Push/pull", exact: true }).click();
    const panel = canvas.getByRole("region", { name: "Modeling" });
    const box = canvas.getByRole("region", { name: "Parts" }).getByRole("textbox", { name: "Select by ID" });
    await box.fill(flat.id);
    await box.press("Enter");
    await panel.getByRole("textbox", { name: "Distance (− pushes in)", exact: true }).fill("1");
    await panel.getByRole("button", { name: "Add push/pull" }).click();
    await expect.poll(async () => (await history()).steps.length, { timeout: 30_000 }).toBe(1);
    const [tag] = await edits();
    expect(tag).toBeTruthy();
    await toolbar.getByRole("button", { name: "Push/pull", exact: true }).click(); // close the panel
    // Clear the face pick, so Claude's message carries no selection.
    await page.getByTestId("viewer").focus();
    await page.keyboard.press("Escape");
    await expect(page.getByTestId("viewer")).toHaveAttribute("data-selected", "");

    // Claude: moves the openings.
    const composer = chat.getByRole("textbox", { name: "Message" });
    await composer.fill(MOVE_OPENINGS);
    await composer.press("Enter");
    await expect.poll(async () => (await get<{ status: string }>(request, api)).status, { timeout: 60_000 }).toBe("idle");
    await expect.poll(async () => (await history()).steps.length, { timeout: 30_000 }).toBe(2);
    const [mine, claudes] = (await history()).steps;
    expect(mine).toMatchObject({ actor: "user", text: "You: push/pull 1 mm" });
    expect(claudes.actor).toBe("claude");
    expect(claudes.text).toMatch(/^Claude: /);
    const undo = toolbar.getByRole("button", { name: "Undo" });
    const redo = toolbar.getByRole("button", { name: "Redo" });
    await expect(undo).toBeEnabled({ timeout: 30_000 });
    await expect(undo).toHaveAttribute("title", `Undo: ${claudes.text} (⌘Z)`);

    // Undo: Claude's run first, then my edit; the toast says which, with Redo.
    const toast = canvas.getByRole("status", { name: "Design history" });
    await undo.click();
    await expect(toast).toContainText(`Undid: ${claudes.text}`, { timeout: 30_000 });
    await expect(toast.getByRole("button", { name: "Redo" })).toBeVisible();
    expect((await history()).position).toBe(1);
    expect(await edits()).toEqual([tag]);
    await shot(page, "undo-toast");
    await expect(undo).toBeEnabled({ timeout: 30_000 });
    await page.getByTestId("viewer").focus();
    await page.keyboard.press("ControlOrMeta+z");
    await expect(toast).toContainText(`Undid: ${mine.text}`, { timeout: 30_000 });
    expect((await history()).position).toBe(0);
    expect(await edits()).toEqual([]);
    await expect(undo).toBeDisabled();
    await expect(undo).toHaveAttribute("title", "Nothing to undo");

    // Redo: my edit, then Claude's run.
    await expect(redo).toBeEnabled({ timeout: 30_000 });
    await redo.click();
    await expect(toast).toContainText(`Redid: ${mine.text}`, { timeout: 30_000 });
    expect(await edits()).toEqual([tag]);
    await expect(redo).toBeEnabled({ timeout: 30_000 });
    await page.getByTestId("viewer").focus();
    await page.keyboard.press("Shift+ControlOrMeta+z");
    await expect(toast).toContainText(`Redid: ${claudes.text}`, { timeout: 30_000 });
    const after = await history();
    expect(after.position).toBe(2);
    expect(after.steps.map((s) => s.text)).toEqual([mine.text, claudes.text]);
    await expect(redo).toBeDisabled();
  });
});

test.describe("on the mock API", () => {
  test("a failed build is shown plainly over the last good model, and a comment goes to the model shown", async ({ page }) => {
    const { request } = page;
    const session = await openExample(request);
    const api = `/api/sessions/${session.id}`;
    await request.post(`${api}/messages`, { data: { text: SESSION.prompt } });
    await expect.poll(async () => (await get<{ status: string }>(request, api)).status, { timeout: 30_000 }).not.toBe("running");
    await answerOpenQuestion(page, api);
    const { viewer, canvas, chat } = await open(page, session.id);
    const good = (await get<Build[]>(request, `${api}/builds`)).at(-1)!;
    expect(good.status).toBe("ready");
    await expect(viewer).toHaveAttribute("data-build", good.id);

    // Claude's change doesn't build.
    const composer = chat.getByRole("textbox", { name: "Message" });
    await composer.fill("Break the build");
    await composer.press("Enter");
    const banner = canvas.getByRole("alert", { name: "Build failed" });
    await expect(banner).toBeVisible({ timeout: 30_000 });
    const failed = (await get<Build[]>(request, `${api}/builds`)).at(-1)!;
    expect(failed.status).toBe("failed");
    await expect(banner).toContainText(`This change couldn't be built: ${failed.failure}.`);
    await expect(banner).not.toContainText(failed.id);
    await expect(banner.getByRole("button", { name: "Undo" })).toBeVisible();
    await expect(banner.getByRole("button", { name: "Ask Claude to fix" })).toBeVisible();
    // The canvas keeps the last good model.
    await expect(viewer).toHaveAttribute("data-build", good.id);
    await expect(viewer).toHaveAttribute("data-state", "ready");
    await shot(page, "failed-build");

    // A comment goes to the build shown, not the failed one.
    const tool = canvas.getByRole("toolbar", { name: "Comment tools" }).getByRole("button", { name: "Comment", exact: true });
    await tool.click();
    const bounds = (await viewer.boundingBox())!;
    const [x, y, w, h] = ((await viewer.getAttribute("data-model-rect")) ?? "").split(",").map(Number);
    await page.mouse.click(bounds.x + x + w * 0.6, bounds.y + y + h * 0.5);
    const draft = canvas.getByRole("dialog", { name: "New comment" });
    await draft.getByRole("textbox", { name: "Comment text" }).fill("Thicker here");
    const posted = page.waitForResponse((r) => r.request().method() === "POST" && /\/comments$/.test(r.url()));
    await draft.getByRole("button", { name: "Comment", exact: true }).click();
    const response = await posted;
    expect(response.status()).toBe(201);
    expect(response.request().postDataJSON().build_id).toBe(good.id);
    await expect(canvas.getByText(/build-\w/)).toHaveCount(0);

    // Undo takes the broken change back: the banner goes.
    await banner.getByRole("button", { name: "Undo" }).click();
    await expect(canvas.getByRole("status", { name: "Design history" })).toContainText("Undid: Claude: ", { timeout: 30_000 });
    await expect(banner).toBeHidden();
  });
});
