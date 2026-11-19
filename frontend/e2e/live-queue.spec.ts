import { mkdirSync } from "node:fs";
import { resolve } from "node:path";
import { expect, test, type Locator, type Page } from "@playwright/test";

// The message queue against the real backend with a scripted Claude (backend/app/e2e_api.py):
// a message starting with "Take your time" replies only after a few seconds, so the test can
// send more while Claude works. A new chat has no board, so "make a box ..." gets a box and any
// other request a question. Screenshots (sample data) go to test-results/screenshots/message-queue/.
test.skip(!!process.env.E2E_BASE_URL, "the scripted backend runs only with the local dev servers");
// One at a time: their designs share the backend's one build slot.
test.describe.configure({ mode: "serial" });

const SLOW = "Take your time";
const BOX = "make a box 40 x 30 x 20 mm";
const BRACKET = "an L bracket with two M4 holes";
const SHOTS = resolve(__dirname, "../test-results/screenshots/message-queue");

test.beforeEach(async ({ context, baseURL }) => {
  await context.addCookies([{ name: "e2e-backend", value: "live", url: baseURL! }]);
});

async function open(page: Page) {
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto("/new");
  await expect(page.locator("[data-shell-ready=true]")).toBeAttached({ timeout: 90_000 });
  return page.getByRole("region", { name: "Chat" });
}

async function say(chat: Locator, text: string) {
  await chat.getByRole("textbox", { name: "Message" }).fill(text);
  await chat.getByRole("button", { name: "Send" }).click();
}

const shot = async (page: Page, name: string) => {
  if (!process.env.QUEUE_SCREENSHOTS) return;
  mkdirSync(SHOTS, { recursive: true });
  await page.screenshot({ path: `${SHOTS}/${name}.png` });
};

const userTexts = (chat: Locator) => chat.getByTestId("user-message").allInnerTexts();

/** Answers "Keep it" if the reply ends asking whether to keep a design that fails a standard. */
async function keepIfAsked(reply: Locator) {
  const keep = reply.getByRole("button", { name: /^Keep it/ });
  const done = reply.locator("xpath=self::*[not(@aria-busy)]"); // the reply ended without asking
  await keep.or(done).first().waitFor({ timeout: 90_000 });
  if (await keep.isVisible()) await keep.click();
}

test("messages sent while Claude works are queued, editable, and sent in order after the run", async ({ page }) => {
  test.setTimeout(240_000);
  const chat = await open(page);
  const queue = chat.getByRole("region", { name: "Queued messages" });
  let posts = 0;
  page.on("request", (r) => {
    if (r.method() === "POST" && /\/messages$/.test(r.url())) posts++;
  });

  await say(chat, `${SLOW} over the first one`);
  const stop = chat.getByRole("button", { name: "Stop" });
  await expect(stop).toBeVisible({ timeout: 90_000 });
  // The composer stays usable: Send queues.
  await expect(chat.getByRole("textbox", { name: "Message" })).toHaveAttribute("placeholder", "Queue a message...");
  await say(chat, BOX);
  await say(chat, "Never mind this one");
  await say(chat, "a bracket");
  await expect(queue.getByRole("heading", { name: "Queued · 3" })).toBeVisible();
  await expect(queue.getByTestId("queued-message")).toHaveText([BOX, "Never mind this one", "a bracket"].map((t) => new RegExp(t)));
  await shot(page, "queued");

  // Cancel one, and edit another before it starts.
  await queue.getByTestId("queued-message").nth(1).getByRole("button", { name: "Remove queued message" }).click();
  const last = queue.getByTestId("queued-message").nth(1);
  await last.getByRole("button", { name: "Edit queued message" }).click();
  await shot(page, "editing");
  await last.getByRole("textbox", { name: "Edit queued message" }).fill(BRACKET);
  await last.getByRole("button", { name: "Save" }).click();
  await expect(queue.getByTestId("queued-message")).toHaveText([new RegExp(BOX), new RegExp(BRACKET)]);
  // Only the first message was posted; the rest are queued on the server.
  expect(posts).toBe(1);

  // Each runs after the one before it, in order, as a normal message.
  // The scripted Claude asks before finishing a design that fails a standard (the box has no
  // draft): the question pauses the queue until it is answered.
  const replies = chat.getByTestId("assistant-message");
  const box = replies.filter({ hasText: "I designed an open box" });
  await expect(box).toBeVisible({ timeout: 90_000 });
  await keepIfAsked(box);
  const bracket = replies.filter({ hasText: "I designed an L bracket" });
  await expect(bracket).toBeVisible({ timeout: 90_000 });
  await keepIfAsked(bracket);
  await expect(queue).toHaveCount(0);
  const asked = (texts: string[]) => texts.filter((t) => t !== "Keep it");
  expect(asked(await userTexts(chat))).toEqual([`${SLOW} over the first one`, BOX, BRACKET]);
  const sid = new URL(page.url()).pathname.split("/").pop()!;
  type Stored = { role: string; text: string; answer?: unknown };
  const stored = (await (await page.request.get(`/api/sessions/${sid}/messages`)).json()) as Stored[];
  expect(stored.filter((m) => m.role === "user" && !m.answer).map((m) => m.text)).toEqual([`${SLOW} over the first one`, BOX, BRACKET]);
  await shot(page, "sent-in-order");
});

test("a question pauses the queue until it is answered or the next message is sent anyway", async ({ page }) => {
  test.setTimeout(240_000);
  const chat = await open(page);
  const queue = chat.getByRole("region", { name: "Queued messages" });

  await say(chat, SLOW);
  await expect(chat.getByRole("button", { name: "Stop" })).toBeVisible({ timeout: 90_000 });
  await say(chat, "something vague"); // Claude asks what to design
  await say(chat, BOX);
  const asked = chat.getByRole("group", { name: "What should I design?" });
  await expect(asked).toBeVisible({ timeout: 90_000 });

  // The queued box is not sent, so it can't be taken as the answer: the queue says it waits.
  await expect(queue.getByText("Paused until you answer Claude's question")).toBeVisible();
  await expect(queue.getByTestId("queued-message")).toHaveText([new RegExp(BOX)]);
  const sid = new URL(page.url()).pathname.split("/").pop()!;
  const session = async () => (await (await page.request.get(`/api/sessions/${sid}`)).json()) as { status: string };
  expect((await session()).status).toBe("waiting_for_answer");
  await page.waitForTimeout(500);
  await expect(queue.getByTestId("queued-message")).toHaveCount(1);
  await shot(page, "paused");

  // Send anyway: the question stays unanswered and the box goes as a normal message.
  await queue.getByRole("button", { name: "Send anyway" }).click();
  await expect(chat.getByText("The question was left unanswered")).toBeVisible({ timeout: 90_000 });
  await expect(chat.getByTestId("assistant-message").filter({ hasText: "I designed an open box" })).toBeVisible({ timeout: 90_000 });
  await expect(queue).toHaveCount(0);
  await expect(asked.getByRole("button").first()).toBeDisabled();
  expect(await userTexts(chat)).toEqual([SLOW, "something vague", BOX]);
});

test("Stop ends the running reply and the queue goes on", async ({ page }) => {
  test.setTimeout(240_000);
  const chat = await open(page);
  await say(chat, `${SLOW} and then some`);
  const stop = chat.getByRole("button", { name: "Stop" });
  await expect(stop).toBeVisible({ timeout: 90_000 });
  await say(chat, BOX);
  await shot(page, "running");
  await stop.click();
  // One Stop: the reply ends with the "stopped" message and Retry, and the queue goes on.
  const stopped = chat.getByTestId("run-stopped").filter({ hasText: "You stopped this reply" });
  await expect(stopped).toBeVisible({ timeout: 10_000 });
  await expect(stopped).toHaveAttribute("data-reason", "stopped");
  await expect(chat.getByText("Took my time")).toHaveCount(0);
  await expect(chat.getByTestId("assistant-message").filter({ hasText: "I designed an open box" })).toBeVisible({ timeout: 90_000 });
  expect(await userTexts(chat)).toEqual([`${SLOW} and then some`, BOX]);
});
