import { expect, test } from "@playwright/test";
import { chatOf, live, newSession, open, settle } from "./helpers";

// Spammed and doubled input (docs/security.md): the chat sends one message per intent, an
// empty composer sends nothing, a huge message stays usable, and a burst of sessions all list.
test.skip(!!process.env.E2E_BASE_URL, "the scripted backend runs only with the local dev servers");
test.beforeEach(async ({ context, baseURL }) => {
  test.setTimeout(240_000); // real builds take seconds each, more on a busy machine
  await live(context, baseURL);
});

const isPost = (url: string, method: string) => method === "POST" && /\/api\/sessions\/[^/]+\/messages$/.test(url);

test("an empty or blank message can't be sent", async ({ page }) => {
  await open(page, "/new");
  const chat = chatOf(page);
  const send = chat.getByRole("button", { name: "Send" });
  await expect(send).toBeDisabled();
  await chat.getByRole("textbox", { name: "Message" }).fill("   \n\t ");
  await expect(send).toBeDisabled();
});

test("Send pressed 20 times, and double-clicked, sends one message", async ({ page }) => {
  const posts: string[] = [];
  page.on("request", (r) => {
    if (isPost(r.url(), r.method())) posts.push(r.url());
  });
  const session = await newSession(page.request);
  await open(page, `/session/${session.id}`);
  const chat = chatOf(page);
  await chat.getByRole("textbox", { name: "Message" }).fill("make a box 40 x 30 x 20 mm");
  const send = chat.getByRole("button", { name: "Send" });
  await send.dblclick();
  for (let i = 0; i < 20; i++) await send.click({ force: true, timeout: 1000 }).catch(() => undefined);
  await chat.getByRole("textbox", { name: "Message" }).press("Enter");
  await settle(page.request, session.id);
  const messages = (await (await page.request.get(`/api/sessions/${session.id}/messages`)).json()) as { role: string }[];
  expect(messages.filter((m) => m.role === "user")).toHaveLength(1);
  expect(posts.length).toBe(1);
  // Once in the chat (the header also names the session after the first request).
  await expect(chat.getByTestId("user-message").filter({ hasText: "make a box 40 x 30 x 20 mm" })).toHaveCount(1);
});

test("Enter held down sends one message", async ({ page }) => {
  const session = await newSession(page.request);
  await open(page, `/session/${session.id}`);
  const box = chatOf(page).getByRole("textbox", { name: "Message" });
  await box.fill("make a box 20 x 20 x 20 mm");
  for (let i = 0; i < 20; i++) await box.press("Enter");
  await settle(page.request, session.id);
  const messages = (await (await page.request.get(`/api/sessions/${session.id}/messages`)).json()) as { role: string }[];
  expect(messages.filter((m) => m.role === "user")).toHaveLength(1);
});

test("a 100k-character message is sent, shown and leaves the page responsive", async ({ page }) => {
  const session = await newSession(page.request);
  await open(page, `/session/${session.id}`);
  const chat = chatOf(page);
  const text = "Make it sturdier. ".repeat(6000).slice(0, 100_000);
  await chat.getByRole("textbox", { name: "Message" }).fill(text);
  await chat.getByRole("button", { name: "Send" }).click();
  await settle(page.request, session.id);
  await expect(chat.getByText("Make it sturdier.", { exact: false }).first()).toBeVisible();
  const started = Date.now();
  await chat.getByRole("textbox", { name: "Message" }).fill("still typing");
  expect(Date.now() - started).toBeLessThan(2000);
});

test("emoji, RTL and zero-width text survive the round trip", async ({ page }) => {
  const text = "👩🏽‍🔧 صندوق zero​width ‮rtl‬ end";
  const session = await newSession(page.request);
  await open(page, `/session/${session.id}`);
  const chat = chatOf(page);
  await chat.getByRole("textbox", { name: "Message" }).fill(text);
  await chat.getByRole("button", { name: "Send" }).click();
  await settle(page.request, session.id);
  const messages = (await (await page.request.get(`/api/sessions/${session.id}/messages`)).json()) as { text: string }[];
  expect(messages[0].text).toBe(text);
  await page.reload();
  await expect(chatOf(page).getByTestId("user-message").getByText("صندوق", { exact: false })).toBeVisible();
});

test("20 sessions made at once all show in Recents", async ({ page }) => {
  // The browser has its owner cookie before the burst, as the app always does (it lists the
  // sessions first). A burst from a browser without one would give each request its own owner.
  await page.request.get("/api/sessions");
  const made = await Promise.all(Array.from({ length: 20 }, (_, i) => newSession(page.request, { title: `Burst ${i}` })));
  expect(new Set(made.map((s) => s.id)).size).toBe(20);
  await open(page, "/new");
  const recents = page.getByRole("list", { name: "Recents" });
  for (const s of made) await expect(recents.locator(`[data-session="${s.id}"]`)).toHaveCount(1);
});
