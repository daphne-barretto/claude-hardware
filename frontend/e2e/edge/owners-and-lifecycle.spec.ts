import { expect, test, type BrowserContext, type Page } from "@playwright/test";
import { chatOf, live, newSession, open, settle } from "./helpers";

// Two browsers, many tabs, reloads and closed tabs mid-run, missing sessions and a slow API
// (docs/security.md).
test.skip(!!process.env.E2E_BASE_URL, "the scripted backend runs only with the local dev servers");
test.beforeEach(async ({ context, baseURL }) => {
  test.setTimeout(240_000); // real builds take seconds each, more on a busy machine
  await live(context, baseURL);
});

test("another browser can't see or open my session, its builds or exports", async ({ page, browser, baseURL }) => {
  const mine = await newSession(page.request, { title: "Mine only" });
  const examples = (await (await page.request.get("/api/examples")).json()) as { id: string }[];
  const withBuild = await newSession(page.request, { example: examples[0].id });
  await expect.poll(async () => (await (await page.request.get(`/api/sessions/${withBuild.id}/builds`)).json()).length, { timeout: 60_000 }).toBeGreaterThan(0);
  const build = ((await (await page.request.get(`/api/sessions/${withBuild.id}/builds`)).json()) as { id: string }[])[0];
  const exported = await page.request.post(`/api/sessions/${withBuild.id}/exports`, { data: { build_id: build.id, formats: ["stl"] } });
  expect(exported.status()).toBe(201);
  const file = ((await exported.json()) as { files: { url: string }[] }).files[0].url;

  const other = await browser.newContext();
  await live(other, baseURL);
  const theirs = await other.newPage();
  for (const url of [`/api/sessions/${mine.id}`, `/api/sessions/${withBuild.id}/builds`, `/api/sessions/${withBuild.id}/builds/${build.id}/mesh`, file]) {
    expect((await theirs.request.get(url)).status(), url).toBe(404);
  }
  expect((await theirs.request.post(`/api/sessions/${mine.id}/messages`, { data: { text: "hi" } })).status()).toBe(404);
  const listed = (await (await theirs.request.get("/api/sessions")).json()) as { id: string }[];
  expect(listed.map((s) => s.id)).not.toContain(mine.id);

  await open(theirs, `/session/${mine.id}`);
  await expect(theirs.getByRole("list", { name: "Recents" }).getByText("Mine only")).toHaveCount(0);
  await expect(chatOf(theirs).getByText(/isn't available in this browser/)).toBeVisible();
  await expect(chatOf(theirs).getByRole("button", { name: "Send" })).toBeDisabled();
  await other.close();
  expect(((await (await page.request.get(`/api/sessions/${mine.id}/messages`)).json()) as unknown[]).length).toBe(0);
});

test("a new browser's first page gives it its owner, so parallel first requests share it", async ({ browser, baseURL }) => {
  // Before any script runs: the page itself sets the owner cookie (src/proxy.ts). Otherwise the
  // page's first API requests go out in parallel, each gets its own owner, and the sessions they
  // make scatter between owners ("isn't available in this browser").
  const fresh = await browser.newContext();
  await live(fresh, baseURL);
  const page = await fresh.newPage();
  await page.goto("/new", { waitUntil: "commit" });
  const owners = (await fresh.cookies()).filter((c) => c.name === "hardware_owner");
  expect(owners).toHaveLength(1);
  expect(owners[0]).toMatchObject({ httpOnly: true, sameSite: "Lax", path: "/" });
  const made = await Promise.all(Array.from({ length: 6 }, (_, i) => newSession(page.request, { title: `Parallel ${i}` })));
  const listed = ((await (await page.request.get("/api/sessions")).json()) as { id: string }[]).map((s) => s.id);
  for (const s of made) expect(listed).toContain(s.id);
  expect((await fresh.cookies()).filter((c) => c.name === "hardware_owner").map((c) => c.value)).toEqual([owners[0].value]);
  await fresh.close();
});

const REPLY = "I designed an open box";

/** `count` tabs open on one session; the last one sends. */
async function tabsOnASession(context: BrowserContext, page: Page, count: number) {
  const session = await newSession(page.request);
  const tabs = [page];
  await open(page, `/session/${session.id}`);
  for (let i = 1; i < count; i++) {
    const tab = await context.newPage();
    await tab.goto(`/session/${session.id}`, { timeout: 20_000 }); // fail fast, not by the test timeout
    await expect(tab.locator("[data-shell-ready=true]")).toBeAttached({ timeout: 20_000 });
    tabs.push(tab);
  }
  const chat = chatOf(tabs[count - 1]);
  await chat.getByRole("textbox", { name: "Message" }).fill("make a box 30 x 20 x 10 mm");
  await chat.getByRole("button", { name: "Send" }).click();
  await settle(page.request, session.id);
  await expect(chat.getByTestId("assistant-message").filter({ hasText: REPLY })).toBeVisible({ timeout: 60_000 });
  return tabs;
}

test("two tabs on one session: the other tab shows the reply live", async ({ context, page }) => {
  const tabs = await tabsOnASession(context, page, 2);
  await expect(chatOf(tabs[0]).getByTestId("assistant-message").filter({ hasText: REPLY })).toBeVisible({ timeout: 15_000 });
});

test("two tabs on one session: the other tab shows the user's message live", async ({ context, page }) => {
  // The event stream carries the user's messages too (`user_message`), so every tab shows both sides.
  const tabs = await tabsOnASession(context, page, 2);
  // In the chat, not the header (which names the session after the first request).
  await expect(chatOf(tabs[0]).getByTestId("user-message").filter({ hasText: "make a box 30 x 20 x 10 mm" })).toBeVisible({ timeout: 15_000 });
});

test("ten tabs on one session all load", async ({ context, page }) => {
  // Over HTTP/1.1 a browser opens at most 6 connections per host. Every tab's chat, canvas,
  // comments and change set follow the session through one shared worker's single event stream
  // (src/lib/session-stream.ts), so ten tabs hold one connection, not forty. (In development the
  // dev server holds a connection per tab of its own: a websocket, which doesn't count.)
  await tabsOnASession(context, page, 10);
});

test("reloading mid-run keeps the run and shows its reply", async ({ page }) => {
  const session = await newSession(page.request);
  await open(page, `/session/${session.id}`);
  const chat = chatOf(page);
  await chat.getByRole("textbox", { name: "Message" }).fill("make a box 30 x 20 x 10 mm");
  await chat.getByRole("button", { name: "Send" }).click();
  await expect(chat.getByTestId("assistant-message").first()).toBeVisible({ timeout: 30_000 });
  await page.reload();
  await expect(chatOf(page).getByTestId("assistant-message").filter({ hasText: "I designed an open box" })).toBeVisible({ timeout: 90_000 });
  // Once in the chat (the header also names the session after the first request).
  await expect(chatOf(page).getByTestId("user-message").filter({ hasText: "make a box 30 x 20 x 10 mm" })).toHaveCount(1);
});

test("closing the tab mid-run and reopening shows the finished reply", async ({ context, page }) => {
  const session = await newSession(page.request);
  await open(page, `/session/${session.id}`);
  const chat = chatOf(page);
  await chat.getByRole("textbox", { name: "Message" }).fill("make a box 30 x 20 x 10 mm");
  await chat.getByRole("button", { name: "Send" }).click();
  await expect(chat.getByTestId("assistant-message").first()).toBeVisible({ timeout: 30_000 });
  const request = context.request;
  await page.close();
  await settle(request, session.id);
  const again = await context.newPage();
  await open(again, `/session/${session.id}`);
  await expect(chatOf(again).getByTestId("assistant-message").filter({ hasText: "I designed an open box" })).toBeVisible({ timeout: 30_000 });
  await expect(chatOf(again).getByTestId("assistant-message").first()).not.toHaveAttribute("aria-busy");
});

for (const id of ["session-999999", "does-not-exist", "..%2F..%2Fetc%2Fpasswd", "x".repeat(300)]) {
  test(`a missing session URL says so and points to New (${id.slice(0, 20)})`, async ({ page }) => {
    const errors: string[] = [];
    page.on("pageerror", (e) => errors.push(e.message));
    await open(page, `/session/${id}`);
    const chat = chatOf(page);
    await expect(chat.getByText(/isn't available in this browser/)).toBeVisible();
    await expect(chat.getByText(/Start a new one with New/)).toBeVisible();
    expect(errors).toEqual([]);
  });
}

test("a slow API leaves the page responsive", async ({ page }) => {
  const session = await newSession(page.request);
  await page.route("**/api/**", async (route) => {
    if (route.request().url().includes("/events")) return route.continue();
    await new Promise((r) => setTimeout(r, 4000));
    await route.continue();
  });
  await open(page, `/session/${session.id}`);
  const box = chatOf(page).getByRole("textbox", { name: "Message" });
  const started = Date.now();
  await box.fill("typing while the API is slow");
  await expect(box).toHaveValue("typing while the API is slow");
  expect(Date.now() - started).toBeLessThan(1500);
  await chatOf(page).getByRole("button", { name: "Send" }).click();
  await expect(chatOf(page).getByText("typing while the API is slow")).toBeVisible({ timeout: 2000 });
  await expect(chatOf(page).getByTestId("assistant-message").first()).toBeVisible({ timeout: 60_000 });
});
