import { expect, test, type Page } from "@playwright/test";

// Session names against the real backend with the scripted Claude (backend/app/e2e_api.py):
// the first request names a new session, Claude renames it after the part it designs (the
// header and Recents follow live, without a reload), and the user renames it from the row menu
// in Recents, after which the name is theirs. The dev server sends requests with the cookie
// `e2e-backend=live` to the scripted backend (playwright.config.ts).
test.skip(!!process.env.E2E_BASE_URL, "the scripted backend runs only with the local dev servers");

const BOX = "make a box 40 x 30 x 20 mm with 2 mm walls";

test.beforeEach(async ({ context, baseURL, page }) => {
  await context.addCookies([{ name: "e2e-backend", value: "live", url: baseURL! }]);
  // Take this browser's owner cookie first, so the page's first requests all carry the same one.
  await page.request.get("/api/sessions");
});

async function open(page: Page, path: string) {
  await page.goto(path);
  await expect(page.locator("[data-shell-ready=true]")).toBeAttached({ timeout: 30_000 });
}

test("Claude names the part live, and the user renames it from Recents", async ({ page }) => {
  test.setTimeout(180_000);
  const errors: string[] = [];
  page.on("pageerror", (err) => errors.push(err.message));
  await page.setViewportSize({ width: 1440, height: 900 });
  await open(page, "/new");
  const chat = page.getByRole("region", { name: "Chat" });
  const recents = page.getByRole("complementary", { name: "Sidebar" }).getByRole("list", { name: "Recents" });

  await chat.getByRole("textbox", { name: "Message" }).fill(BOX);
  await chat.getByRole("button", { name: "Send" }).click();
  await page.waitForURL(/\/session\/[^/]+$/, { timeout: 30_000 });
  const sessionId = page.url().split("/").pop()!;
  const row = recents.locator(`[data-session="${sessionId}"]`);

  // The scripted Claude asks before finishing a design that fails a standard: keep it.
  const keep = chat.getByRole("button", { name: /^Keep it/ });
  await keep.or(chat.getByText(/Open box/)).first().waitFor({ timeout: 90_000 });
  if (await keep.isVisible()) await keep.click();

  // Claude named the part: the header and Recents show it without a reload.
  await expect(chat.getByRole("heading", { name: "Open box" })).toBeVisible({ timeout: 90_000 });
  await expect(row).toHaveText(/^Open box/);

  // Rename from the row menu (⋯ → Rename): Enter saves.
  await recents.getByRole("button", { name: "Options for Open box" }).click();
  await page.getByRole("menuitem", { name: "Rename" }).click();
  const field = recents.getByRole("textbox", { name: "Session name" });
  await field.fill("My **desk** box");
  await field.press("Enter");
  await expect(row).toHaveText(/^My desk box/);
  await expect(chat.getByRole("heading", { name: "My desk box" })).toBeVisible();
  await expect(page.getByRole("region", { name: "Canvas" }).getByRole("heading", { name: "My desk box" })).toBeVisible();
  const session = await (await page.request.get(`/api/sessions/${sessionId}`)).json();
  expect(session).toMatchObject({ title: "My desk box", title_locked: true });

  // Escape cancels a rename.
  await recents.getByRole("button", { name: "Options for My desk box" }).click();
  await page.getByRole("menuitem", { name: "Rename" }).click();
  await recents.getByRole("textbox", { name: "Session name" }).fill("Not this");
  await recents.getByRole("textbox", { name: "Session name" }).press("Escape");
  await expect(row).toHaveText(/^My desk box/);
  expect(errors).toEqual([]);
});
