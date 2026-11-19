import { expect, test, type Page } from "@playwright/test";

// Limits and failures against the real backend with a scripted Claude (backend/app/e2e_api.py):
// a message starting "Simulate ..." plays an API failure, and two test-only routes drop a
// session's event stream or restart it. The chat must say plainly what happened, keep the
// work, and offer the way forward. Set LIMITS_SCREENSHOTS=1 to refresh
// frontend/test-results/screenshots/limits/ (sample data only).
test.skip(!!process.env.E2E_BASE_URL && !process.env.E2E_LIVE, "the scripted backend runs only with the local dev servers");

const SHOTS = process.env.LIMITS_SCREENSHOTS ? "test-results/screenshots/limits" : null;

test.beforeEach(async ({ context, baseURL }) => {
  await context.addCookies([{ name: "e2e-backend", value: "live", url: baseURL! }]);
});

async function sendFirst(page: Page, text: string) {
  await page.setViewportSize({ width: 1280, height: 800 });
  await page.goto("/new");
  await expect(page.locator("[data-shell-ready=true]")).toBeAttached({ timeout: 30_000 });
  const chat = page.getByRole("region", { name: "Chat" });
  const created = page.waitForResponse((r) => r.url().endsWith("/api/sessions") && r.request().method() === "POST");
  await chat.getByRole("textbox", { name: "Message" }).fill(text);
  await chat.getByRole("button", { name: "Send" }).click();
  const session = (await (await created).json()) as { id: string };
  return { chat, id: session.id };
}

async function shot(page: Page, name: string) {
  if (SHOTS) await page.getByRole("region", { name: "Chat" }).screenshot({ path: `${SHOTS}/${name}.png` });
}

const RAW = /API Error|429|529|rate_limit_error|credit balance|Traceback|Exception|slot|worker|sandbox/;

test("a rate limit is retried with a visible countdown, then the reply goes through", async ({ page }) => {
  test.setTimeout(90_000);
  const { chat } = await sendFirst(page, "Simulate rate limit");
  const notice = chat.getByTestId("run-notice");
  await expect(notice).toContainText("Claude is getting a lot of requests right now", { timeout: 30_000 });
  await expect(notice).toContainText(/try \d of 2/);
  await expect(notice.getByTestId("retry-countdown")).toContainText(/Retrying in \d s|Retrying…/);
  await expect(notice.getByRole("button", { name: "Stop" })).toBeVisible();
  await shot(page, "rate-limit-retrying");
  await expect(chat.getByText("Claude is back: the reply went through.")).toBeVisible({ timeout: 30_000 });
  await expect(notice).toHaveCount(0);
  await expect(chat.getByTestId("run-stopped")).toHaveCount(0);
  await expect(chat).not.toContainText(RAW);
});

test("out of credits says so plainly and offers Retry", async ({ page }) => {
  const { chat } = await sendFirst(page, "Simulate out of credits");
  const stopped = chat.getByTestId("run-stopped");
  await expect(stopped).toContainText("this app's Anthropic account has run out of credits", { timeout: 30_000 });
  await expect(stopped).toContainText("Your design and builds are kept");
  await expect(stopped).toHaveAttribute("data-reason", "billing");
  await expect(stopped.getByRole("button", { name: "Retry" })).toBeVisible();
  await expect(chat).not.toContainText(RAW);
  await shot(page, "out-of-credits");
  // Retry picks up (the scripted Claude now answers).
  await stopped.getByRole("button", { name: "Retry" }).click();
  await expect(chat.getByText("Picked up where the stopped reply left off.")).toBeVisible({ timeout: 30_000 });
});

test("a dropped event stream reconnects and the chat goes on", async ({ page }) => {
  test.setTimeout(90_000);
  const { chat, id } = await sendFirst(page, "Simulate slow reply");
  await expect(chat.getByText("Working on the slow reply.")).toBeVisible({ timeout: 30_000 });
  const dropped = await page.request.post(`/api/e2e/sessions/${id}/drop-streams`);
  expect(dropped.ok()).toBe(true);
  await expect(chat.getByTestId("connection-status")).toHaveText("Reconnecting…", { timeout: 10_000 });
  await shot(page, "reconnecting");
  await expect(chat.getByTestId("connection-status")).toHaveCount(0, { timeout: 15_000 });
  // Events flow again: stopping the run shows up, and nothing was repeated.
  await page.request.post(`/api/sessions/${id}/stop`);
  await expect(chat.getByTestId("run-stopped")).toContainText("You stopped this reply.", { timeout: 15_000 });
  await expect(chat.getByText("Working on the slow reply.")).toHaveCount(1);
});

test("a server restart mid-run says the reply was interrupted, and Retry carries on", async ({ page }) => {
  test.setTimeout(90_000);
  const { chat, id } = await sendFirst(page, "Simulate slow reply");
  await expect(chat.getByText("Working on the slow reply.")).toBeVisible({ timeout: 30_000 });
  const restarted = await page.request.post(`/api/e2e/sessions/${id}/restart`);
  expect(restarted.ok()).toBe(true);
  const stopped = chat.getByTestId("run-stopped");
  await expect(stopped).toContainText("This reply was interrupted because the server restarted", { timeout: 20_000 });
  await expect(stopped).toContainText("Your design and builds so far are kept");
  await expect(stopped).toHaveAttribute("data-reason", "interrupted");
  await shot(page, "interrupted");
  // On a reload the chat says the same.
  await page.reload();
  await expect(page.getByRole("region", { name: "Chat" }).getByTestId("run-stopped")).toContainText("interrupted", { timeout: 30_000 });
  const again = page.getByRole("region", { name: "Chat" });
  await again.getByTestId("run-stopped").getByRole("button", { name: "Retry" }).click();
  await expect(again.getByText("Picked up where the stopped reply left off.")).toBeVisible({ timeout: 30_000 });
  await expect(again.getByText("Working on the slow reply.")).toHaveCount(1);
  await shot(page, "interrupted-retried");
});
