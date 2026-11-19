import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { expect, test, type Browser, type Page } from "@playwright/test";

// Several people use the app at once, against the real backend with Claude played by
// a script (backend/app/e2e_api.py), as in live-chat.spec.ts. Each browser context is a
// user with its own cookies: its sessions are its own (backend/app/owners.py), and a run
// that finds the run slots taken waits in line, showing its place.
test.skip(!!process.env.E2E_BASE_URL, "the scripted backend runs only with the local dev servers");

const repo = (...parts: string[]) => resolve(__dirname, "../..", ...parts);
const SESSION = JSON.parse(readFileSync(repo("fixtures/session.json"), "utf8")) as { prompt: string };
const NEW_BOARD = /From `[^`]+\.json` to `([^`]+\.json)`/.exec(readFileSync(repo("demos/b-parasite/CHANGES.md"), "utf8"))![1];
const NEW_REVISION = (JSON.parse(readFileSync(repo("demos", "b-parasite", NEW_BOARD), "utf8")) as { revision: string }).revision;
// The scripted backend's run-slot messages (app/e2e_api.py: HOLD_SLOT, TAKE_SLOT, HOLDING).
const HOLD_SLOT = "Hold the run slot";
const TAKE_SLOT = "Take a run slot";
const HOLDING = "Holding the run slot until another run waits for it.";
// Set SCREENSHOTS to save the states this test reaches (frontend/test-results/screenshots/several-users/).
const SHOTS = process.env.SCREENSHOTS ? resolve(__dirname, "../test-results/screenshots/several-users") : null;

async function user(browser: Browser, baseURL: string) {
  const context = await browser.newContext({ baseURL, viewport: { width: 1440, height: 900 } });
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

/** Opens a new chat and sends `text`; the session it made. */
async function start(page: Page, text: string): Promise<string> {
  await open(page, "/new");
  const chat = page.getByRole("region", { name: "Chat" });
  const created = page.waitForResponse((r) => r.url().endsWith("/api/sessions") && r.request().method() === "POST");
  await chat.getByRole("textbox", { name: "Message" }).fill(text);
  await chat.getByRole("button", { name: "Send" }).click();
  return ((await (await created).json()) as { id: string }).id;
}

const reply = (page: Page, text: string) =>
  page.getByRole("region", { name: "Chat" }).getByTestId("assistant-message").filter({ hasText: text });

test("two people running the demo at once never see each other's sessions or builds", async ({ browser, baseURL }) => {
  test.setTimeout(180_000);
  const a = await user(browser, baseURL!);
  const b = await user(browser, baseURL!);
  const [mine, theirs] = await Promise.all([start(a.page, SESSION.prompt), start(b.page, SESSION.prompt)]);
  expect(mine).not.toBe(theirs);

  // Both demo runs stream their builds and reach the question, each in its own chat.
  for (const { page } of [a, b]) {
    const run = reply(page, "Run with model");
    await expect(run.getByTestId("build-ready")).toContainText(NEW_REVISION, { timeout: 60_000 });
    await expect(run.getByRole("group").getByRole("button").first()).toBeEnabled({ timeout: 60_000 });
  }

  // Each user's API sees only its own session; the other's is not found.
  const ids = async (page: Page) => ((await (await page.request.get("/api/sessions")).json()) as { id: string }[]).map((s) => s.id);
  expect(await ids(a.page)).toEqual([mine]);
  expect(await ids(b.page)).toEqual([theirs]);
  for (const path of ["", "/builds", "/messages"]) {
    expect((await b.page.request.get(`/api/sessions/${mine}${path}`)).status()).toBe(404);
    expect((await a.page.request.get(`/api/sessions/${theirs}${path}`)).status()).toBe(404);
  }
  const builds = async (page: Page, id: string) => ((await (await page.request.get(`/api/sessions/${id}/builds`)).json()) as unknown[]).length;
  expect(await builds(a.page, mine)).toBeGreaterThan(0);
  expect(await builds(b.page, theirs)).toBeGreaterThan(0);

  // Opening the other user's session shows neither its chat nor its builds.
  await open(b.page, `/session/${mine}`);
  await expect(b.page.getByTestId("session-unavailable")).toBeVisible({ timeout: 15_000 });
  await expect(b.page.getByTestId("assistant-message")).toHaveCount(0);
  await expect(b.page.getByTestId("viewer")).toHaveCount(0);
  if (SHOTS) await b.page.screenshot({ path: `${SHOTS}/2-another-users-session.png` });

  // The session survives a page reload: its run, build and open question come back.
  await open(a.page, `/session/${mine}`);
  for (let i = 0; i < 2; i++) {
    const run = reply(a.page, "Run with model");
    await expect(run.getByTestId("build-ready")).toContainText(NEW_REVISION, { timeout: 30_000 });
    await expect(run.getByRole("group").getByRole("button").first()).toBeEnabled({ timeout: 30_000 });
    await expect(a.page.getByTestId("session-unavailable")).toHaveCount(0);
    await a.page.reload();
    await expect(a.page.locator("[data-shell-ready=true]")).toBeAttached({ timeout: 30_000 });
  }
  expect([...a.errors, ...b.errors]).toEqual([]);
  await Promise.all([a.context.close(), b.context.close()]);
});

test("a run started while the server is busy waits in line and shows its place", async ({ browser, baseURL }) => {
  test.setTimeout(180_000);
  const a = await user(browser, baseURL!);
  const b = await user(browser, baseURL!);

  // The first user's run takes the only run slot and keeps it while another waits.
  await start(a.page, HOLD_SLOT);
  await expect(reply(a.page, HOLDING)).toBeVisible({ timeout: 30_000 });

  // The second user's run waits in line, first, and says so in the chat.
  await start(b.page, TAKE_SLOT);
  // A waiting notice (backend app.agent.limits.waiting_text), until the run starts.
  const queued = b.page.getByTestId("run-notice").filter({ hasText: "You're next. Claude starts on this as soon as" });
  await expect(queued).toBeVisible({ timeout: 30_000 });
  if (SHOTS) await b.page.screenshot({ path: `${SHOTS}/1-run-waits-in-line.png` });

  // Then it runs, once the first has finished.
  await expect(reply(a.page, HOLDING)).toContainText("Had a run slot.", { timeout: 60_000 });
  await expect(reply(b.page, "Had a run slot.")).toBeVisible({ timeout: 60_000 });
  await expect(queued).toHaveCount(0);
  await expect(reply(b.page, "Had a run slot.")).not.toHaveAttribute("aria-busy", { timeout: 30_000 });
  expect([...a.errors, ...b.errors]).toEqual([]);
  await Promise.all([a.context.close(), b.context.close()]);
});
