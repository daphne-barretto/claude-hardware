import { expect, test, type Page } from "@playwright/test";

// docs/security.md in the browser, against the real backend with a scripted Claude
// (backend/app/e2e_api.py): the page's security headers hold while three.js renders a real
// build, a pasted key never reaches the chat, and a rate limit shows its plain message.
// It sends messages, so it never runs against a deployed app (that would call the real model)
// unless E2E_SECURITY_LOCAL says the base URL is a local dev server on the scripted backend.
test.skip(!!process.env.E2E_BASE_URL && !process.env.E2E_SECURITY_LOCAL, "needs the scripted backend");

const SHOTS = process.env.SCREENSHOT_DIR;
const KEY = "sk-ant-api03-" + "Ab3".repeat(15);
const REMOVED_NOTE =
  "We removed what looked like a key, token or email address from your message before saving it or sending it to Claude, to keep it private.";

test.beforeEach(async ({ context, baseURL }) => {
  await context.addCookies([{ name: "e2e-backend", value: "live", url: baseURL! }]);
});

async function open(page: Page, path: string) {
  await page.goto(path);
  await expect(page.locator("[data-shell-ready=true]")).toBeAttached({ timeout: 30_000 });
}

test("pages carry the security headers, and a real build renders under the CSP", async ({ page }) => {
  test.setTimeout(120_000);
  const violations: string[] = [];
  page.on("console", (m) => {
    if (/Content.Security.Policy|Refused to/i.test(m.text())) violations.push(m.text());
  });
  const response = await page.goto("/new");
  const headers = response!.headers();
  expect(headers["content-security-policy"]).toContain("frame-ancestors 'none'");
  expect(headers["x-frame-options"]).toBe("DENY");
  expect(headers["x-content-type-options"]).toBe("nosniff");
  expect(headers["referrer-policy"]).toBe("same-origin");
  expect(headers["x-powered-by"]).toBeUndefined();

  // An example session's first build, rendered by three.js on the session page.
  const examples = (await (await page.request.get("/api/examples")).json()) as { id: string }[];
  const session = (await (await page.request.post("/api/sessions", { data: { example: examples[0].id } })).json()) as { id: string };
  await expect
    .poll(async () => ((await (await page.request.get(`/api/sessions/${session.id}/builds`)).json()) as unknown[]).length, { timeout: 60_000 })
    .toBeGreaterThan(0);
  await open(page, `/session/${session.id}`);
  await expect(page.getByTestId("viewer")).toHaveAttribute("data-state", "ready", { timeout: 30_000 });
  expect(violations).toEqual([]);
});

test("a request from another site is refused", async ({ page }) => {
  const r = await page.request.post("/api/sessions", { data: {}, headers: { Origin: "https://evil.example" } });
  expect(r.status()).toBe(403);
  expect((await r.json()).detail).toBe("This request came from another website, so it was blocked. Open the app directly and try again.");
});

test("a pasted key is removed before it is saved, and the chat says so", async ({ page }) => {
  test.setTimeout(90_000);
  await page.setViewportSize({ width: 1280, height: 800 });
  await open(page, "/new");
  const chat = page.getByRole("region", { name: "Chat" });
  await chat.getByRole("textbox", { name: "Message" }).fill(`Here is my key ${KEY}, can you make a box?`);
  await chat.getByRole("button", { name: "Send" }).click();
  await expect(chat.getByText(REMOVED_NOTE)).toBeVisible({ timeout: 30_000 });
  await expect(chat.getByText("Here is my key [removed: API key], can you make a box?")).toBeVisible();
  await expect(chat).not.toContainText(KEY);
  if (SHOTS) await page.screenshot({ path: `${SHOTS}/removed-key.png` });
  // Reloaded from the server, the message is the scrubbed one.
  const id = page.url().split("/session/")[1] ?? (await (await page.request.get("/api/sessions")).json())[0]?.id;
  if (id) {
    const messages = (await (await page.request.get(`/api/sessions/${id}/messages`)).json()) as { text: string }[];
    expect(JSON.stringify(messages)).not.toContain(KEY);
  }
});

test("over the message limit, the chat shows the limit's plain message", async ({ page }) => {
  test.setTimeout(90_000);
  await page.setViewportSize({ width: 1280, height: 800 });
  await open(page, "/new");
  // This browser's owner has used its 10 messages this minute (some refused while busy: they
  // count too). page.request shares the browser's cookie.
  const session = (await (await page.request.post("/api/sessions", { data: {} })).json()) as { id: string };
  for (let n = 0; n < 10; n++) await page.request.post(`/api/sessions/${session.id}/messages`, { data: { text: `hello ${n}` } });
  const chat = page.getByRole("region", { name: "Chat" });
  await chat.getByRole("textbox", { name: "Message" }).fill("Make a box 40 x 30 x 20 mm with 2 mm walls");
  await chat.getByRole("button", { name: "Send" }).click();
  await expect(chat.getByText(/^You're sending messages faster than this demo allows \(10 a minute\)\. Please wait a moment and try again in /)).toBeVisible({
    timeout: 30_000,
  });
  await expect(chat.getByText(/Could not reach Claude/)).toHaveCount(0);
  if (SHOTS) await page.screenshot({ path: `${SHOTS}/message-limit.png` });
});
