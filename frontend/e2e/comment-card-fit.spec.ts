import { expect, test, type Locator, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";
import path from "node:path";
import { answerOpenQuestion } from "./inventory/ui-inventory";

// A comment thread is anchored at its pin. Pinned low on the model in a short window, the card ran
// past the canvas, under the checks strip and the Layout bar, so the options of Claude's question
// in it couldn't be reached (a ~1075 x 660 window). Every card anchored to the model stays in the
// canvas between its top bar and its bottom bar (components/canvas/fitPanel.ts, `useAnchoredCard`),
// and scrolls inside when it is taller. The mock API asks in the thread for a comment starting
// "Which way" (backend/app/mock_api.py).

test.skip(!!process.env.E2E_BASE_URL, "the mock API runs only with the local dev servers");

const SESSION = JSON.parse(readFileSync(path.resolve(__dirname, "../../fixtures/session.json"), "utf8")) as { prompt: string };

async function openSession(page: Page, size: { width: number; height: number }) {
  const session = (await (await page.request.post("/api/sessions", { data: {} })).json()) as { id: string };
  const api = `/api/sessions/${session.id}`;
  await page.request.post(`${api}/messages`, { data: { text: SESSION.prompt } });
  await expect.poll(async () => ((await (await page.request.get(api)).json()) as { status: string }).status, { timeout: 30_000 }).not.toBe("running");
  await answerOpenQuestion(page, api);
  await page.setViewportSize(size);
  await page.goto(`/session/${session.id}`);
  const viewer = page.getByTestId("viewer");
  await expect(viewer).toHaveAttribute("data-state", "ready", { timeout: 30_000 });
  return { viewer, canvas: page.getByRole("region", { name: "Canvas" }) };
}

/** The canvas between its top bar and its bottom bar, on screen. */
const band = (page: Page) =>
  page.evaluate(() => {
    const host = document.querySelector('[data-testid="hardware-canvas"]') as HTMLElement;
    const r = host.getBoundingClientRect();
    const px = (name: string) => {
      const probe = document.createElement("div");
      probe.style.cssText = `position:absolute;visibility:hidden;height:var(${name},0px)`;
      host.appendChild(probe);
      const h = probe.getBoundingClientRect().height;
      probe.remove();
      return h;
    };
    return { left: r.left, right: r.right, top: r.top + px("--chrome-top"), bottom: r.bottom - px("--chrome-bottom") };
  });

async function expectInBand(page: Page, loc: Locator, what: string) {
  const b = await band(page);
  const box = (await loc.boundingBox())!;
  expect(box.y, `${what}: top`).toBeGreaterThanOrEqual(b.top - 0.5);
  expect(box.y + box.height, `${what}: bottom`).toBeLessThanOrEqual(b.bottom + 0.5);
  expect(box.x, `${what}: left`).toBeGreaterThanOrEqual(b.left - 0.5);
  expect(box.x + box.width, `${what}: right`).toBeLessThanOrEqual(b.right + 0.5);
}

for (const size of [
  { width: 1280, height: 800 },
  { width: 1075, height: 660 },
]) {
  test(`a thread pinned low keeps Claude's question and its options inside the canvas at ${size.width}x${size.height}`, async ({ page }) => {
    const { viewer, canvas } = await openSession(page, size);
    // Pin low on the model and send it to Claude, who asks how to go on in the thread.
    await canvas.getByRole("toolbar", { name: "Comment tools" }).getByRole("button", { name: "Comment", exact: true }).click();
    const box = (await viewer.boundingBox())!;
    const [x, y, w, h] = ((await viewer.getAttribute("data-model-rect")) ?? "").split(",").map(Number);
    // As low on the model as a click lands on a face.
    const draft = canvas.getByRole("dialog", { name: "New comment" });
    for (const at of [0.9, 0.85, 0.8, 0.75, 0.7]) {
      await page.mouse.click(box.x + x + w * 0.5, box.y + y + h * at);
      if (await draft.isVisible({ timeout: 1_000 }).catch(() => false)) break;
    }
    await expect(draft).toBeVisible();
    await expectInBand(page, draft, "the new comment card");
    await draft.getByRole("textbox", { name: "Comment text" }).fill("Which way: make this 1ft tall");
    await draft.getByRole("button", { name: "Send to Claude", exact: true }).click();

    const thread = canvas.getByRole("dialog", { name: "Comment thread" });
    const question = thread.getByTestId("comment-question");
    await expect(question).toBeVisible({ timeout: 30_000 });
    await expectInBand(page, thread, "the thread card");
    // The header stays in view; each option scrolls into the card and can be clicked there.
    await expectInBand(page, thread.getByRole("button", { name: "Close thread" }), "Close thread");
    const options = question.getByRole("button");
    await expect(options).toHaveCount(3);
    for (let i = 0; i < 3; i++) {
      const option = options.nth(i);
      await option.scrollIntoViewIfNeeded();
      await expectInBand(page, option, `option ${i + 1}`);
      await option.click({ trial: true }); // nothing covers it
    }
  });
}
