import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { expect, test, type Browser, type Page } from "@playwright/test";

// Share links, against the real backend with Claude played by a script (backend/app/e2e_api.py),
// as in live-users.spec.ts: each browser context has its own cookies, so its sessions are
// its own (backend/app/owners.py). The owner shares a session; another browser opens the link,
// sees the chat and the model read-only, and continues from it in a session of its own; a
// design shared as a copy opens as an editable copy; a link the owner stops sharing is off.
test.skip(!!process.env.E2E_BASE_URL, "the scripted backend runs only with the local dev servers");

const repo = (...parts: string[]) => resolve(__dirname, "../..", ...parts);
const SESSION = JSON.parse(readFileSync(repo("fixtures/session.json"), "utf8")) as { prompt: string };
// Set SCREENSHOTS to save the states this test reaches (frontend/test-results/screenshots/share-links/).
const SHOTS = process.env.SCREENSHOTS ? resolve(__dirname, "../test-results/screenshots/share-links") : null;

async function person(browser: Browser, baseURL: string) {
  const context = await browser.newContext({ baseURL, viewport: { width: 1440, height: 900 } });
  await context.grantPermissions(["clipboard-read", "clipboard-write"], { origin: baseURL });
  await context.addCookies([{ name: "e2e-backend", value: "live", url: baseURL }]);
  const page = await context.newPage();
  const errors: string[] = [];
  page.on("pageerror", (err) => errors.push(err.message));
  return { context, page, errors };
}

async function open(page: Page, path: string) {
  await page.goto(path);
  await expect(page.locator("[data-shell-ready=true]")).toBeAttached({ timeout: 30_000 });
}

const shot = async (page: Page, name: string) => {
  if (SHOTS) await page.screenshot({ path: `${SHOTS}/${name}.png` });
};

const reply = (page: Page, text: string) =>
  page.getByRole("region", { name: "Chat" }).getByTestId("assistant-message").filter({ hasText: text });

/** Opens the Share menu and creates the link of a kind; the link's path. */
async function createLink(page: Page, section: "Share chat" | "Share a copy"): Promise<string> {
  const canvas = page.getByRole("region", { name: "Canvas" });
  const menu = page.getByRole("dialog", { name: "Share" });
  if (!(await menu.isVisible())) await canvas.getByRole("button", { name: "Share" }).click();
  const part = menu.getByRole("region", { name: section });
  await part.getByRole("button", { name: "Create link" }).click();
  await expect(part.getByRole("button", { name: "Link copied" })).toBeVisible();
  const href = (await part.getByTestId(section === "Share chat" ? "share-url-view" : "share-url-copy").textContent())!;
  expect(href).toMatch(/\/share\/[0-9a-f]{48}$/);
  return new URL(href).pathname;
}

test("an owner shares a session; another browser views it read-only, continues from it, and the link turns off", async ({
  browser,
  baseURL,
}) => {
  test.setTimeout(240_000);
  const owner = await person(browser, baseURL!);
  const viewer = await person(browser, baseURL!);

  // The owner's session on the first example, with a reply, a build and an open question.
  const [example] = (await (await owner.page.request.get("/api/examples")).json()) as { id: string }[];
  const made = (await (await owner.page.request.post("/api/sessions", { data: { example: example.id } })).json()) as { id: string };
  await open(owner.page, `/session/${made.id}`);
  const chat = owner.page.getByRole("region", { name: "Chat" });
  await chat.getByRole("textbox", { name: "Message" }).fill(SESSION.prompt);
  await chat.getByRole("button", { name: "Send" }).click();
  await expect(reply(owner.page, "Run with model").getByTestId("build-ready")).toBeVisible({ timeout: 60_000 });
  await expect(owner.page.getByTestId("viewer")).toHaveAttribute("data-state", "ready", { timeout: 60_000 });
  const ownSession = new URL(owner.page.url()).pathname;

  const viewLink = await createLink(owner.page, "Share chat");
  await expect(owner.page.getByTestId("share-status-view")).toContainText("Link is live");
  await shot(owner.page, "1-owner-share-menu");

  // Another browser can't open the session itself, but can open the link: chat and model, read-only.
  await open(viewer.page, ownSession);
  await expect(viewer.page.getByTestId("session-unavailable")).toBeVisible({ timeout: 15_000 });
  await open(viewer.page, viewLink);
  await expect(viewer.page.getByTestId("shared-notice")).toContainText("Shared by link · read-only", { timeout: 30_000 });
  await expect(reply(viewer.page, "Run with model")).toBeVisible({ timeout: 30_000 });
  await expect(viewer.page.getByTestId("viewer")).toHaveAttribute("data-state", "ready", { timeout: 60_000 });
  await expect(viewer.page.getByRole("textbox", { name: "Message" })).toHaveCount(0);
  await shot(viewer.page, "2-viewer-read-only");
  const token = viewLink.split("/").pop()!;
  for (const [method, path] of [
    ["POST", "/messages"],
    ["PATCH", "/parameters/wall"],
    ["POST", "/comments"],
  ] as const) {
    const r = await viewer.page.request.fetch(`/api/shares/${token}${path}`, { method, data: { text: "x" } });
    expect(r.status()).toBe(409);
  }
  // Viewing doesn't make it one of the viewer's sessions.
  const own = async (page: Page) =>
    ((await (await page.request.get("/api/sessions")).json()) as { id: string; sample?: boolean }[]).filter((s) => !s.sample).map((s) => s.id);
  expect(await own(viewer.page)).toEqual([]);

  // Continue from this: a session of the viewer's own, with the chat, that takes messages.
  await viewer.page.getByRole("button", { name: "Continue from this" }).click();
  await expect(viewer.page).toHaveURL(/\/session\/session-\d+$/, { timeout: 30_000 });
  await expect(reply(viewer.page, "Run with model")).toBeVisible({ timeout: 30_000 });
  await expect(viewer.page.getByRole("textbox", { name: "Message" })).toBeVisible();
  const copied = new URL(viewer.page.url()).pathname.split("/").pop()!;
  expect(await own(viewer.page)).toEqual([copied]);
  expect((await owner.page.request.get(`/api/sessions/${copied}`)).status()).toBe(404);
  await shot(viewer.page, "3-continued-copy");

  // Share a copy: the design alone, which opens as the opener's own editable copy.
  const copyLink = await createLink(owner.page, "Share a copy");
  await shot(owner.page, "4-owner-both-links");
  await open(viewer.page, copyLink);
  await expect(viewer.page.getByRole("heading", { name: /· shared design/ })).toBeVisible({ timeout: 30_000 });
  await expect(viewer.page.getByTestId("viewer")).toHaveAttribute("data-state", "ready", { timeout: 60_000 });
  await shot(viewer.page, "5-shared-design-landing");
  await viewer.page.getByRole("button", { name: "Open a copy" }).click();
  await expect(viewer.page).toHaveURL(/\/session\/session-\d+$/, { timeout: 30_000 });
  await expect(reply(viewer.page, "Started from a shared design.")).toBeVisible({ timeout: 30_000 });
  await expect(reply(viewer.page, "Run with model")).toHaveCount(0);
  await expect(viewer.page.getByTestId("viewer")).toHaveAttribute("data-state", "ready", { timeout: 60_000 });
  await expect(viewer.page.getByRole("textbox", { name: "Message" })).toBeVisible();
  expect((await own(viewer.page)).length).toBe(2);
  await shot(viewer.page, "6-opened-copy");

  // Stop sharing: the link is off for everyone.
  await owner.page.getByRole("region", { name: "Share chat" }).getByRole("button", { name: "Stop sharing" }).click();
  await expect(owner.page.getByRole("region", { name: "Share chat" }).getByRole("button", { name: "Create link" })).toBeVisible();
  await open(viewer.page, viewLink);
  await expect(viewer.page.getByTestId("share-link-message")).toContainText("This link was turned off", { timeout: 30_000 });
  await shot(viewer.page, "7-link-turned-off");

  expect(owner.errors).toEqual([]);
  expect(viewer.errors).toEqual([]);
  await owner.context.close();
  await viewer.context.close();
});
