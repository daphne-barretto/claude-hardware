import { readFileSync } from "node:fs";
import path from "node:path";
import { expect, test, type APIRequestContext, type Page } from "@playwright/test";

// Questions and the can't-satisfy state against the mock session API: the conflict
// fixture (fixtures/conflict-events.jsonl), played for session.json's conflict_prompt.
// Scenario values come from the fixture files, never from here.
test.skip(!!process.env.E2E_BASE_URL, "the mock API runs only with the local dev servers");

type Location = { point: [number, number, number]; part?: string | null; faces?: string[] };
type Constraint = { kind: string; id: string; label: string; locations: Location[] };
type Question = {
  type: "question";
  question_id: string;
  text: string;
  options: { id: string; label: string; description?: string | null }[];
  location: Location;
  conflict: { over_by: number; constraints: Constraint[]; location: Location };
};

const root = path.join(__dirname, "../..");
const SESSION = JSON.parse(readFileSync(path.join(root, "fixtures/session.json"), "utf8")) as { conflict_prompt: string };
const QUESTION = readFileSync(path.join(root, "fixtures/conflict-events.jsonl"), "utf8")
  .split("\n")
  .filter(Boolean)
  .map((line) => JSON.parse(line).event)
  .find((e) => e.type === "question") as Question;

/** A mock session that ran into the conflict and is waiting for an answer, opened at /session/{id}. */
async function openConflict(page: Page, request: APIRequestContext) {
  const session = await (await request.post("/api/sessions", { data: {} })).json();
  await request.post(`/api/sessions/${session.id}/messages`, { data: { text: SESSION.conflict_prompt } });
  await expect
    .poll(async () => (await (await request.get(`/api/sessions/${session.id}`)).json()).status, { timeout: 30_000 })
    .toBe("waiting_for_answer");
  await page.goto(`/session/${session.id}`);
  await expect(page.locator("[data-shell-ready=true]")).toBeAttached({ timeout: 30_000 });
  await expect(page.getByTestId("viewer")).toHaveAttribute("data-state", "ready", { timeout: 30_000 });
  const chat = page.getByRole("region", { name: "Chat" });
  const canvas = page.getByRole("region", { name: "Canvas" });
  return { id: session.id as string, chat, canvas, asked: chat.getByRole("group", { name: QUESTION.text }) };
}

const optionName = (o: Question["options"][number]) => new RegExp(`^${escape(o.label)} `);
const escape = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

test("the conflict fixture shows the card with the clashing constraints, their locations and the options", async ({
  page,
  request,
}) => {
  const errors: string[] = [];
  page.on("pageerror", (err) => errors.push(err.message));
  const { chat, canvas, asked } = await openConflict(page, request);

  // In the chat: the question, the card and the options.
  const card = asked.getByRole("region", { name: "Conflict" });
  const items = card.getByRole("list", { name: "Clashing constraints" }).getByRole("listitem");
  await expect(items).toHaveCount(QUESTION.conflict.constraints.length);
  for (const [i, c] of QUESTION.conflict.constraints.entries()) {
    const item = items.nth(i);
    await expect(item).toHaveAttribute("data-id", c.id);
    await expect(item.getByTestId("constraint-text")).toContainText(c.label);
    const locations = item.getByTestId("constraint-location");
    await expect(locations).toHaveCount(c.locations.length);
    for (const [j, l] of c.locations.entries()) {
      await expect(locations.nth(j)).toContainText(l.part!);
      await expect(locations.nth(j)).toContainText(l.faces![0]);
    }
  }
  await expect(card.getByTestId("conflict-over-by")).toContainText(`${QUESTION.conflict.over_by} mm apart`);
  await expect(asked.getByRole("button")).toHaveCount(QUESTION.options.length);
  for (const o of QUESTION.options) await expect(asked.getByRole("button", { name: optionName(o) })).toBeEnabled();

  // On the canvas: pinned at the spot, with the same card and options.
  const pin = canvas.getByTestId("question-pin");
  await expect(pin).toHaveAttribute("data-point", QUESTION.location.point.join(","));
  const viewer = page.getByTestId("viewer");
  const [rx, ry, rw, rh] = (await viewer.getAttribute("data-model-rect"))!.split(",").map(Number);
  const x = Number(await pin.getAttribute("data-x"));
  const y = Number(await pin.getAttribute("data-y"));
  expect(x).toBeGreaterThanOrEqual(rx);
  expect(x).toBeLessThanOrEqual(rx + rw);
  expect(y).toBeGreaterThanOrEqual(ry);
  expect(y).toBeLessThanOrEqual(ry + rh);
  const marker = canvas.getByRole("button", { name: "Claude's question" });
  const box = (await marker.boundingBox())!;
  const view = (await viewer.boundingBox())!;
  expect(Math.abs(box.x + box.width / 2 - (view.x + x))).toBeLessThan(2);
  expect(Math.abs(box.y + box.height / 2 - (view.y + y))).toBeLessThan(2);

  const popover = canvas.getByRole("region", { name: "Question on the model" });
  await expect(popover).toContainText(QUESTION.text);
  await expect(popover.getByRole("listitem")).toHaveCount(QUESTION.conflict.constraints.length);
  await expect(popover.getByRole("button")).toHaveCount(QUESTION.options.length);
  await expect(marker).toHaveAttribute("aria-expanded", "true");
  // The card stays inside the canvas.
  const cardBox = (await popover.boundingBox())!;
  expect(cardBox.x).toBeGreaterThanOrEqual(view.x);
  expect(cardBox.x + cardBox.width).toBeLessThanOrEqual(view.x + view.width + 1);
  expect(cardBox.y + cardBox.height).toBeLessThanOrEqual(view.y + view.height + 1);
  await page.screenshot({ path: test.info().outputPath("conflict.png") });

  // The marker closes and reopens the card; the pin follows the view.
  await marker.click();
  await expect(popover).toBeHidden();
  await marker.click();
  await expect(popover).toBeVisible();
  await marker.focus();
  await page.keyboard.press("2"); // the top view shortcut
  await expect(viewer).toHaveAttribute("data-view", "top");
  await expect(pin).not.toHaveAttribute("data-y", String(y));
  await expect(chat).toBeVisible();
  expect(errors).toEqual([]);
});

test("choosing an option in the chat continues the run", async ({ page, request }) => {
  const { id, chat, canvas, asked } = await openConflict(page, request);
  const option = QUESTION.options[0];
  const answered = page.waitForRequest((r) => r.url().endsWith("/messages") && r.method() === "POST");
  await asked.getByRole("button", { name: optionName(option) }).click();
  expect((await answered).postDataJSON()).toMatchObject({
    answer: { question_id: QUESTION.question_id, option_id: option.id },
  });
  // Claude's reply streams in, the question is answered and the pin is gone.
  await expect(chat.getByTestId("assistant-message")).toHaveCount(2);
  await expect(chat.getByTestId("assistant-message").nth(1)).toContainText(option.label);
  await expect(asked.getByRole("button", { name: optionName(option) })).toHaveAttribute("aria-pressed", "true");
  await expect(canvas.getByTestId("question-pin")).toHaveCount(0);
  await expect
    .poll(async () => (await (await request.get(`/api/sessions/${id}`)).json()).status, { timeout: 30_000 })
    .toBe("idle");
});

test("choosing an option on the pin continues the run", async ({ page, request }) => {
  const { chat, canvas, asked } = await openConflict(page, request);
  const option = QUESTION.options.at(-1)!;
  const popover = canvas.getByRole("region", { name: "Question on the model" });
  const answered = page.waitForRequest((r) => r.url().endsWith("/messages") && r.method() === "POST");
  await popover.getByRole("button", { name: optionName(option) }).click();
  expect((await answered).postDataJSON()).toMatchObject({
    answer: { question_id: QUESTION.question_id, option_id: option.id },
  });
  await expect(chat.getByTestId("assistant-message").nth(1)).toContainText(option.label);
  // The chat shows the same answer.
  await expect(asked.getByRole("button", { name: optionName(option) })).toHaveAttribute("aria-pressed", "true");
  await expect(canvas.getByTestId("question-pin")).toHaveCount(0);
});
