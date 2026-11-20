import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { expect, test, type Page } from "@playwright/test";
import { DEFAULT_CHOICES, EFFORTS, MODELS } from "../src/components/sites/claude-shell/data";

// The chat panel against the real backend: the session API and the agent loop with its
// tools, hooks, builds and checks, with Claude played by a script (backend/app/e2e_api.py)
// because CI has no API key. The script writes back what reached the run (model, effort, mode,
// selected faces), so the test sees the choices arrive at the agent, not only in the request.
// The dev server sends requests with the cookie `e2e-backend=live` to it (playwright.config.ts).
test.skip(!!process.env.E2E_BASE_URL, "the scripted backend runs only with the local dev servers");

const repo = (...parts: string[]) => resolve(__dirname, "../..", ...parts);
const SESSION = JSON.parse(readFileSync(repo("fixtures/session.json"), "utf8")) as { prompt: string };
const NEW_BOARD = /From `[^`]+\.json` to `([^`]+\.json)`/.exec(readFileSync(repo("demos/b-parasite/CHANGES.md"), "utf8"))![1];
const NEW_REVISION = (JSON.parse(readFileSync(repo("demos", "b-parasite", NEW_BOARD), "utf8")) as { revision: string }).revision;

type Message = { role: string; text: string; model?: string; effort?: string; mode?: string; selection?: { faces: string[] }; answer?: { option_id: string } };

test.beforeEach(async ({ context, baseURL }) => {
  await context.addCookies([{ name: "e2e-backend", value: "live", url: baseURL! }]);
});

async function open(page: Page, path: string) {
  await page.goto(path);
  await expect(page.locator("[data-shell-ready=true]")).toBeAttached({ timeout: 30_000 });
}

const messagePosted = (page: Page) =>
  page.waitForRequest((r) => r.method() === "POST" && /\/api\/sessions\/[^/]+\/messages$/.test(r.url()));

test("the requests go to the real backend, not the mock", async ({ page }) => {
  const health = await (await page.request.get("/api/health")).json();
  expect(health.status).toBe("ok");
  expect(health.mock).toBeUndefined();
});

test("send, stream and answer a live run, then ask about a selected face", async ({ page }) => {
  test.setTimeout(120_000);
  const errors: string[] = [];
  page.on("pageerror", (err) => errors.push(err.message));
  await page.setViewportSize({ width: 1440, height: 900 });
  await open(page, "/new");
  const chat = page.getByRole("region", { name: "Chat" });
  const model = MODELS.find((m) => m.id !== DEFAULT_CHOICES.model)!;
  const effort = EFFORTS.find((e) => e.id !== DEFAULT_CHOICES.effort)!;

  // Choose another model and another effort.
  const picker = chat.getByRole("button", { name: /Fable|Opus|Sonnet|Haiku/ });
  await picker.click();
  await chat.getByRole("menuitemradio", { name: new RegExp(model.name) }).click();
  await picker.click();
  await chat.getByRole("menuitem", { name: /Effort/ }).click();
  await chat.getByRole("menuitemradio", { name: new RegExp(`^${effort.name}`) }).click();
  await expect(picker).toContainText(model.name);
  await expect(picker).toContainText(effort.name);

  // Send.
  const created = page.waitForResponse((r) => r.url().endsWith("/api/sessions") && r.request().method() === "POST");
  const sent = messagePosted(page);
  await chat.getByRole("textbox", { name: "Message" }).fill(SESSION.prompt);
  await chat.getByRole("button", { name: "Send" }).click();
  const session = (await (await created).json()) as { id: string };
  expect((await sent).postDataJSON()).toMatchObject({ text: SESSION.prompt, model: model.id, effort: effort.id, mode: "auto" });

  // Stream: the agent's progress lines and text, a build with its checks, then the question.
  // (The session's first build, of the existing enclosure, arrives as a reply of its own.)
  const reply = chat.getByTestId("assistant-message").filter({ hasText: "Run with model" });
  await expect(reply).toContainText(`Run with model ${model.id}, effort ${effort.id}, mode auto.`, { timeout: 30_000 });
  await expect(reply.getByTestId("progress-line").filter({ hasText: `Reading board revision ${NEW_REVISION}` })).toBeVisible();
  await expect(reply.getByTestId("build-ready")).toContainText(NEW_REVISION, { timeout: 30_000 });
  const asked = reply.getByRole("group");
  await expect(asked.getByRole("button").first()).toBeEnabled({ timeout: 30_000 });
  await expect(reply).not.toHaveAttribute("aria-busy");
  const status = async () => (await (await page.request.get(`/api/sessions/${session.id}`)).json()).status;
  expect(await status()).toBe("waiting_for_answer");

  // The open question doesn't hold the composer: a message typed meanwhile can be sent (the API
  // takes it and tells Claude the question was left open, `test_open_questions.py`).
  const send = chat.getByRole("button", { name: "Send" });
  await chat.getByRole("textbox", { name: "Message" }).fill("Something else first");
  await expect(send).toBeEnabled();
  await chat.getByRole("textbox", { name: "Message" }).fill("");

  // Answer with an option button: the paused run resumes and ends.
  const option = asked.getByRole("button").nth(1);
  const optionLabel = await option.locator("span span").first().innerText();
  const answered = messagePosted(page);
  await option.click();
  expect((await answered).postDataJSON()).toMatchObject({ text: "", model: model.id, effort: effort.id, mode: "auto" });
  await expect(option).toHaveAttribute("aria-pressed", "true");
  await expect.poll(status, { timeout: 30_000 }).toBe("idle");
  await expect(reply).not.toHaveAttribute("aria-busy");

  // The answer shows before the reply's continuation: the resumed run streams in below it.
  const items = chat.locator("[data-testid=user-message], [data-testid=assistant-message]");
  const order = () => items.evaluateAll((els) => els.map((el) => el.getAttribute("data-testid")));
  await expect.poll(async () => (await order()).slice(-2)).toEqual(["user-message", "assistant-message"]);
  await expect(items.nth(-2)).toHaveText(optionLabel);
  const continuation = items.last();
  await expect(continuation).not.toHaveAttribute("aria-busy");
  await expect(continuation.getByTestId("progress-line").last()).toContainText(/Stopped|Done|Finished/i);
  // The paused part ends at its question: nothing from after the answer streamed in above it.
  await expect(reply.locator("> *").last()).toHaveAttribute("role", "group");

  // The real endpoint stored both messages with the choices they were sent with.
  let messages = (await (await page.request.get(`/api/sessions/${session.id}/messages`)).json()) as Message[];
  expect(messages.filter((m) => m.role === "user")).toMatchObject([
    { text: SESSION.prompt, model: model.id, effort: effort.id, mode: "auto" },
    { model: model.id, effort: effort.id, mode: "auto", answer: { option_id: expect.any(String) } },
  ]);

  // Reopened, the session resends its events. The stream has no event for the answer, but the
  // run went on past the question, so its options can't be chosen and say why.
  await open(page, `/session/${session.id}`);
  const reopened = chat.getByRole("group", { name: await asked.getAttribute("aria-label") ?? "" });
  await expect(reopened.getByRole("button").first()).toBeVisible({ timeout: 30_000 });
  await expect(chat.getByTestId("assistant-message").last()).not.toHaveAttribute("aria-busy");
  for (const button of await reopened.getByRole("button").all()) {
    await expect(button).toBeDisabled();
    await expect(button).toHaveAttribute("title", "Claude already went on past this question.");
  }
  await expect(chat.getByRole("button", { name: "Send" })).not.toHaveAttribute("title", "Answer Claude's question first");

  // Select a face on the session's build and ask about it: its ID goes with the next request.
  const viewer = page.getByTestId("viewer");
  await expect(viewer).toHaveAttribute("data-state", "ready", { timeout: 30_000 });
  const box = (await viewer.boundingBox())!;
  await page.mouse.click(box.x + box.width / 2, box.y + box.height / 2);
  const selected = async () => ((await viewer.getAttribute("data-selected")) ?? "").split(",").filter(Boolean);
  await expect.poll(selected).toHaveLength(1);
  const [faceId] = await selected();
  await expect(chat.getByTestId("selection-chip")).toBeVisible();

  const asking = messagePosted(page);
  await chat.getByRole("textbox", { name: "Message" }).fill("Why is this face here?");
  await chat.getByRole("button", { name: "Send" }).click();
  expect((await asking).postDataJSON().selection.faces).toEqual([faceId]);
  // The face reached Claude's prompt in the run.
  await expect(chat.getByTestId("assistant-message").last()).toContainText(`Selected faces: ${faceId}.`, { timeout: 30_000 });
  await expect.poll(status, { timeout: 30_000 }).toBe("idle");
  messages = (await (await page.request.get(`/api/sessions/${session.id}/messages`)).json()) as Message[];
  expect(messages.filter((m) => m.role === "user").at(-1)).toMatchObject({ text: "Why is this face here?", selection: { faces: [faceId] } });
  expect(errors).toEqual([]);
});
