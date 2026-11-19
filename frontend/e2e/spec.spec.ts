import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { parse } from "yaml";
import { expect, test, type Locator, type Page } from "@playwright/test";

// Spec, constraints and the build plan against the mock session API: a description
// becomes constraints, listed in the canvas's Constraints panel and traced to the constraint set or
// to the user's words (with a line under the message in chat); editing a constraint, or going
// back to the set's value, rebuilds and rechecks; a new design shows Claude's plan first.
test.skip(!!process.env.E2E_BASE_URL, "the mock API runs only with the local dev servers");

type Standard = { id: string; title: string; kind: string; value: number | string; unit?: "mm" | "deg"; aliases: string[] };
const root = resolve(__dirname, "../..");
const standards = (parse(readFileSync(resolve(root, "standards.yaml"), "utf8")) as { standards: Standard[] }).standards;
const numeric = standards.filter((s) => s.kind !== "spec");
const recorded = readFileSync(resolve(root, "fixtures/session-events.jsonl"), "utf8")
  .split("\n")
  .filter(Boolean)
  .map((line) => JSON.parse(line).event);
const plan = recorded.find((e) => e.type === "plan") as { steps: string[] };
const question = recorded.find((e) => e.type === "question") as { text: string; options: { label: string }[] };

/** "2.5 mm wall": the standard named with a value other than its own. */
const other = (s: Standard) => Math.round((Number(s.value) + 0.5) * 1000) / 1000;
const phrase = (s: Standard, value = other(s)) => `${value}${s.unit === "deg" ? "°" : ` ${s.unit}`} ${s.aliases[0]}`;

async function describe(page: Page, text: string) {
  await page.goto("/new");
  await expect(page.locator("[data-shell-ready=true]")).toBeAttached({ timeout: 30_000 });
  const chat = page.getByRole("region", { name: "Chat" });
  await chat.getByRole("textbox", { name: "Message" }).fill(text);
  await chat.getByRole("button", { name: "Send" }).click();
  return chat;
}

async function answer(page: Page) {
  const chat = page.getByRole("region", { name: "Chat" });
  const asked = chat.getByRole("group", { name: question.text });
  const option = asked.getByRole("button", { name: new RegExp(`^${question.options[0].label} `) });
  // The recorded run streams its build and checks before it asks.
  await expect(option).toBeEnabled({ timeout: 30_000 });
  await option.click();
  await expect(chat.getByTestId("assistant-message")).toHaveCount(2);
  await expect(chat.getByTestId("assistant-message").nth(1)).not.toHaveAttribute("aria-busy");
  // The build is on the canvas, with its check results in the bar.
  await expect(page.getByTestId("viewer")).toHaveAttribute("data-state", "ready", { timeout: 30_000 });
}

const canvasOf = (page: Page) => page.getByRole("region", { name: "Canvas" });
const item = (page: Page, s: Standard) => canvasOf(page).locator(`[data-testid=constraint-item][data-constraint="${s.id}"]`);

/** Expands a constraint's row in the Constraints panel (opened from the canvas toolbar once there is a build). */
async function openDetail(page: Page, s: Standard): Promise<Locator> {
  if (!(await canvasOf(page).getByTestId("constraints-panel").isVisible()))
    await canvasOf(page).getByRole("toolbar", { name: "Canvas controls" }).getByRole("button", { name: "Constraints" }).click();
  const it = item(page, s);
  if ((await it.getAttribute("aria-expanded")) !== "true") await it.click();
  const detail = canvasOf(page).getByTestId("constraint-detail");
  await expect(detail).toHaveAttribute("data-constraint", s.id);
  return detail;
}

test("a description produces constraints in the canvas that say where each came from", async ({ page }) => {
  const target = numeric[0];
  const chat = await describe(page, `A small sensor case with ${phrase(target)}`);
  // In the canvas as soon as the message is sent: the empty canvas shows the panel open, not checked yet.
  const bar = canvasOf(page).getByRole("list", { name: "Constraints" });
  await expect(canvasOf(page).getByTestId("constraints-panel")).toBeVisible();
  await expect(item(page, target)).toBeVisible();
  await expect(item(page, target)).toContainText(String(other(target)));
  // Chat says, in one line, what it took from the user's words.
  await expect(chat.getByTestId("user-message").getByTestId("constraint-note")).toContainText(String(other(target)));

  await expect(page.getByTestId("viewer")).toHaveAttribute("data-state", "ready", { timeout: 30_000 });
  await openDetail(page, target);
  await expect(bar.locator('[data-status="fail"]').first()).toBeVisible();
  const keys = await bar.getByTestId("constraint-item").evaluateAll((els) => els.map((e) => e.getAttribute("data-constraint")!));
  expect(keys.length).toBeGreaterThan(1);
  for (const s of standards.filter((x) => keys.includes(x.id))) {
    const detail = await openDetail(page, s);
    const source = detail.getByTestId("constraint-source");
    if (s.id === target.id) {
      await expect(source).toContainText(`From your words`);
      await expect(source).toContainText(phrase(s));
      await expect(detail.getByRole("button", { name: "Back to the set's value" })).toBeAttached();
    } else {
      await expect(source).toHaveText(new RegExp(`^Constraint ${s.id} in `));
      await expect(detail.getByRole("button", { name: "Back to the set's value" })).toHaveCount(0);
    }
  }
});

test("a new design shows Claude's build plan before its question", async ({ page }) => {
  const chat = await describe(page, "Make an enclosure for this board");
  const shown = chat.getByRole("group", { name: "Build plan" });
  await expect(shown.getByRole("listitem")).toHaveText(plan.steps);
  await expect(chat.getByRole("group", { name: question.text })).toBeVisible();
  // While Claude waits for the answer, the constraints say why they can't change.
  const detail = await openDetail(page, numeric[0]);
  const edit = detail.getByRole("button", { name: `Edit ${numeric[0].title}` });
  await expect(edit).toBeDisabled();
  await expect(edit).toHaveAccessibleDescription(/Answer Claude's question first/);
  // Answering continues the run.
  await answer(page);
  await expect((await openDetail(page, numeric[0])).getByRole("button", { name: `Edit ${numeric[0].title}` })).toBeEnabled();
});

test("editing a constraint rebuilds and rechecks", async ({ page }) => {
  const target = numeric[numeric.length - 1];
  const chat = await describe(page, "Make an enclosure for this board");
  await answer(page);
  const detail = await openDetail(page, target);
  await detail.getByRole("button", { name: `Edit ${target.title}` }).click();
  const value = other(target);
  await detail.getByRole("textbox", { name: target.title }).fill(String(value));
  const patched = page.waitForRequest((r) => r.method() === "PATCH" && r.url().includes(`/constraints/${target.id}`));
  await detail.getByRole("button", { name: `Save ${target.title}` }).click();
  expect((await patched).postDataJSON()).toEqual({ value });
  const recheck = chat.getByTestId("assistant-message").nth(2);
  await expect(recheck.getByTestId("progress-line")).toContainText(/Rebuilding and rechecking/);
  await expect(recheck.getByTestId("build-ready")).toBeVisible();
  await expect(item(page, target)).toContainText(String(value));
  await expect((await openDetail(page, target)).getByTestId("constraint-source")).toContainText(/^Edited by you/);
});

test("going back to the set's value drops the user's words, rebuilt and rechecked", async ({ page }) => {
  const target = numeric[0];
  const chat = await describe(page, `A case with ${phrase(target)}`);
  await answer(page);
  const deleted = page.waitForRequest((r) => r.method() === "DELETE" && r.url().includes(`/constraints/${target.id}`));
  await (await openDetail(page, target)).getByRole("button", { name: "Back to the set's value" }).click();
  await deleted;
  const recheck = chat.getByTestId("assistant-message").nth(2);
  await expect(recheck.getByTestId("progress-line")).toContainText(/Back to the set's value/);
  await expect(recheck.getByTestId("build-ready")).toBeVisible();
  const detail = await openDetail(page, target);
  await expect(detail.getByTestId("constraint-source")).toHaveText(new RegExp(`^Constraint ${target.id} in `));
  await expect(detail.getByRole("button", { name: "Back to the set's value" })).toHaveCount(0);
});

test("Escape cancels an edit without sending it, then closes the detail", async ({ page }) => {
  const target = numeric[0];
  await describe(page, "Make an enclosure for this board");
  await answer(page);
  let sent = false;
  page.on("request", (r) => (sent ||= r.method() === "PATCH"));
  const detail = await openDetail(page, target);
  await detail.getByRole("button", { name: `Edit ${target.title}` }).click();
  await detail.getByRole("textbox", { name: target.title }).fill("99");
  await page.keyboard.press("Escape");
  await expect(detail.getByRole("textbox", { name: target.title })).toHaveCount(0);
  await expect(detail).toBeVisible();
  expect(sent).toBe(false);
  await detail.getByRole("button", { name: `Edit ${target.title}` }).focus();
  await page.keyboard.press("Escape");
  await expect(detail).toBeHidden();
  await expect(item(page, target)).toBeFocused();
  // Escape again closes the panel.
  await page.keyboard.press("Escape");
  await expect(canvasOf(page).getByTestId("constraints-panel")).toBeHidden();
});
