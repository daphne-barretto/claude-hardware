import { readdirSync, readFileSync } from "node:fs";
import { resolve } from "node:path";
import { expect, test, type Page } from "@playwright/test";
import { DEFAULT_CHOICES, EFFORTS, MODELS } from "../src/components/sites/claude-shell/data";

// The chat panel against the mock session API (docs/architecture.md): send a message with the
// composer's choices, watch the recorded run stream in, and answer Claude's question.
test.skip(!!process.env.E2E_BASE_URL, "the mock API runs only with the local dev servers");

type Recorded = { event: { type: string; text?: string; delta?: string; question_id?: string; options?: { id: string; label: string }[] } };
const recorded: Recorded["event"][] = readFileSync(resolve(__dirname, "../../fixtures/session-events.jsonl"), "utf8")
  .split("\n")
  .filter(Boolean)
  .map((line) => (JSON.parse(line) as Recorded).event);
const progressLines = recorded.filter((e) => e.type === "progress").map((e) => e.text!);
const question = recorded.find((e) => e.type === "question")!;
const lastText = recorded.filter((e) => e.type === "text").at(-1)!.delta!.trim();

async function open(page: Page, path: string) {
  await page.goto(path);
  await expect(page.locator("[data-shell-ready=true]")).toBeAttached({ timeout: 30_000 });
}

test("send, stream and answer against the mock API", async ({ page, request }) => {
  const errors: string[] = [];
  page.on("pageerror", (err) => errors.push(err.message));
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

  // Send: the choices go with the message.
  const created = page.waitForResponse((r) => r.url().endsWith("/api/sessions") && r.request().method() === "POST");
  const sent = page.waitForRequest((r) => r.url().endsWith("/messages") && r.method() === "POST");
  await chat.getByRole("textbox", { name: "Message" }).fill("Check the enclosure against the new revision");
  await chat.getByRole("button", { name: "Send" }).click();
  const session = await (await created).json();
  expect((await sent).postDataJSON()).toMatchObject({
    text: "Check the enclosure against the new revision",
    model: model.id,
    effort: effort.id,
    mode: "auto",
  });

  // Stream: progress lines in order, the streamed text, then the question.
  const reply = chat.getByTestId("assistant-message").first();
  await expect(reply.getByTestId("progress-line")).toHaveText(progressLines);
  await expect(reply.getByTestId("progress-line").first()).toContainText(/round 1 of \d+/);
  await expect(reply).toContainText(lastText);
  // The recorded builds: the existing enclosure on its own revision and on the new one.
  const builds = reply.getByTestId("build-ready");
  await expect(builds).toHaveCount(recorded.filter((e) => e.type === "build_ready").length);
  for (const b of await builds.all()) await expect(b).toBeVisible();
  const asked = reply.getByRole("group", { name: question.text });
  await expect(asked.getByRole("button")).toHaveCount(question.options!.length);
  await expect(reply).not.toHaveAttribute("aria-busy");

  // Answer with an option button.
  const option = question.options![1];
  const answered = page.waitForRequest((r) => r.url().endsWith("/messages") && r.method() === "POST");
  await asked.getByRole("button", { name: new RegExp(option.label) }).click();
  expect((await answered).postDataJSON()).toMatchObject({
    answer: { question_id: question.question_id, option_id: option.id },
    model: model.id,
    effort: effort.id,
    mode: "auto",
  });
  await expect(asked.getByRole("button", { name: new RegExp(option.label) })).toHaveAttribute("aria-pressed", "true");
  await expect(asked.getByRole("button", { disabled: true })).toHaveCount(question.options!.length);
  // Claude's reply to the answer streams in as a second message.
  await expect(chat.getByTestId("assistant-message")).toHaveCount(2);
  await expect(chat.getByTestId("assistant-message").nth(1)).toContainText(option.label);

  // The API stored both messages with the choices they were sent with.
  const messages = await (await request.get(`/api/sessions/${session.id}/messages`)).json();
  const fromUser = messages.filter((m: { role: string }) => m.role === "user");
  expect(fromUser).toMatchObject([
    { model: model.id, effort: effort.id, mode: "auto" },
    { model: model.id, effort: effort.id, mode: "auto", answer: { option_id: option.id } },
  ]);
  expect(errors).toEqual([]);
});

test("answered question options look disabled and say why", async ({ page }) => {
  await open(page, "/new");
  const chat = page.getByRole("region", { name: "Chat" });
  await chat.getByRole("textbox", { name: "Message" }).fill("Check the enclosure against the new revision");
  await chat.getByRole("button", { name: "Send" }).click();
  const asked = chat.getByRole("group", { name: question.text });
  const chosen = asked.getByRole("button", { name: new RegExp(`^${question.options![0].label} `) });
  await expect(chosen).toBeEnabled();
  await chosen.click();
  await expect(chosen).toHaveAttribute("aria-pressed", "true");
  await expect(asked.getByRole("button", { disabled: true })).toHaveCount(question.options!.length);
  for (const option of await asked.getByRole("button").all()) {
    await expect(option).toHaveAccessibleDescription(/answered/i);
    const { opacity, cursor } = await option.evaluate((el) => ({
      opacity: Number(getComputedStyle(el).opacity),
      cursor: getComputedStyle(el).cursor,
    }));
    expect(opacity <= 0.6 || cursor === "not-allowed", `${await option.textContent()} looks disabled`).toBe(true);
  }
  // The chosen option still shows it was chosen.
  await expect(chosen.getByTestId("chosen-mark")).toBeVisible();
});

test("an attached STEP file is uploaded and sent with the message", async ({ page, request }) => {
  // Files are STEP models or images: the example's case model, from demos/b-parasite/.
  const demo = resolve(__dirname, "../../demos/b-parasite");
  const boardName = readdirSync(demo).find((f) => f.endsWith(".step"))!;
  const board = readFileSync(resolve(demo, boardName));

  await open(page, "/new");
  const chat = page.getByRole("region", { name: "Chat" });
  const chooser = page.waitForEvent("filechooser");
  await chat.getByRole("button", { name: "Add", exact: true }).click();
  await chat.getByRole("menuitem", { name: "Add files or photos" }).click();
  await (await chooser).setFiles({ name: boardName, mimeType: "application/octet-stream", buffer: board });
  await expect(chat.getByRole("list", { name: "Attached files" })).toContainText(boardName);

  const uploaded = page.waitForResponse((r) => r.url().endsWith("/attachments") && r.request().method() === "POST");
  const sent = page.waitForRequest((r) => r.url().endsWith("/messages") && r.method() === "POST");
  await chat.getByRole("textbox", { name: "Message" }).fill("Here is the case model");
  await chat.getByRole("button", { name: "Send" }).click();
  const upload = await uploaded;
  expect(upload.status()).toBe(201);
  const attachment = await upload.json();
  expect(attachment).toMatchObject({ name: boardName });
  expect((await sent).postDataJSON()).toMatchObject({ text: "Here is the case model", attachments: [attachment] });

  // The posted message shows the file, the composer is clear, and the API has the file itself.
  await expect(chat.getByTestId("user-message").last()).toContainText(boardName);
  await expect(chat.getByRole("list", { name: "Attached files" })).toBeHidden();
  const sessionId = /\/sessions\/([^/]+)\//.exec(upload.url())![1];
  const messages = await (await request.get(`/api/sessions/${sessionId}/messages`)).json();
  expect(messages[0]).toMatchObject({ role: "user", attachments: [attachment] });
  expect(Buffer.from(await (await request.get(attachment.url)).body()).equals(board)).toBe(true);
});
