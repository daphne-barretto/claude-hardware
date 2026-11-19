import { readFileSync } from "node:fs";
import path from "node:path";
import { expect, test, type APIRequestContext, type Page } from "@playwright/test";
import { openExample } from "./fixtures/sessions";

// Tracked changes against the mock session API: the fixture change set
// (fixtures/change-set.json, one change per demos/b-parasite/CHANGES.md row) on the canvas, with
// accept and reject. Scenario values come from the fixture files, never from here.
test.skip(!!process.env.E2E_BASE_URL, "the mock API runs only with the local dev servers");

type Change = { id: string; title: string; status: string; delta: { kind: string; move?: [number, number] | null } };
const fixture = (name: string) => JSON.parse(readFileSync(path.join(__dirname, "../../fixtures", name), "utf8"));
const CHANGE_SET = fixture("change-set.json") as { changes: Change[] };
const SESSION = fixture("session.json") as { prompt: string };
const EVENTS = readFileSync(path.join(__dirname, "../../fixtures/session-events.jsonl"), "utf8")
  .split("\n")
  .filter(Boolean)
  .map((line) => JSON.parse(line).event);
const QUESTION = EVENTS.find((e) => e.type === "question");

/** A mock session whose run has proposed the fixture change set, opened on the canvas. */
async function openProposal(page: Page, request: APIRequestContext) {
  const session = await openExample(request);
  const api = `/api/sessions/${session.id}`;
  await request.post(`${api}/messages`, { data: { text: SESSION.prompt } });
  await expect
    .poll(async () => (await (await request.get(api)).json()).status, { timeout: 30_000 })
    .toBe("waiting_for_answer");
  await request.post(`${api}/messages`, {
    data: { text: "", answer: { question_id: QUESTION.question_id, option_id: QUESTION.options[0].id } },
  });
  await expect.poll(async () => (await request.get(`${api}/changes`)).status(), { timeout: 30_000 }).toBe(200);
  await page.goto(`/session/${session.id}`);
  const viewer = page.getByTestId("viewer");
  await expect(viewer).toHaveAttribute("data-state", "ready", { timeout: 30_000 });
  const canvas = page.getByRole("region", { name: "Canvas" });
  // The panel starts collapsed to a button, so it doesn't cover the model.
  const toggle = canvas.getByRole("button", { name: "Proposed changes", exact: true });
  await expect(toggle).toHaveAttribute("aria-expanded", "false");
  return { api, viewer, canvas, toggle, panel: canvas.getByRole("region", { name: "Proposed changes" }) };
}

test("every change in the proposal appears as a pin, a ghost and in the panel", async ({ page, request }) => {
  const { canvas, panel, toggle } = await openProposal(page, request);
  expect(CHANGE_SET.changes.length).toBeGreaterThan(0);
  await expect(canvas.getByTestId("change-pin")).toHaveCount(CHANGE_SET.changes.length);
  await expect(toggle).toHaveText(`${CHANGE_SET.changes.length}Changes`);

  // A pin opens the panel at its row and takes the camera to the change.
  const last = CHANGE_SET.changes.length - 1;
  await canvas.getByRole("button", { name: `Change ${last + 1}: ${CHANGE_SET.changes[last].title}` }).click();
  await expect(toggle).toHaveAttribute("aria-expanded", "true");
  await expect(panel).toBeVisible();
  await expect(page.getByTestId("viewer")).toHaveAttribute("data-zoom", "300");
  await expect(panel.locator(`li[data-change="${CHANGE_SET.changes[last].id}"]`)).toHaveAttribute("data-selected", "true");
  // Escape closes the panel; back to the whole model, to see every pin.
  await page.keyboard.press("Escape");
  await expect(panel).toBeHidden();
  await canvas.getByRole("button", { name: "Home view" }).click();
  await toggle.click();

  const rows = panel.getByRole("list", { name: "Changes to review" }).getByRole("listitem");
  await expect(rows).toHaveCount(CHANGE_SET.changes.length);
  for (const [i, c] of CHANGE_SET.changes.entries()) {
    const pin = canvas.getByRole("button", { name: `Change ${i + 1}: ${c.title}` });
    await expect(pin).toBeVisible();
    await expect(pin).toHaveText(String(i + 1));
    await expect(rows.nth(i)).toHaveAttribute("data-change", c.id);
    await expect(rows.nth(i)).toContainText(c.title);
    await expect(panel.getByRole("button", { name: `Accept change ${i + 1}` })).toBeEnabled();
    await expect(panel.getByRole("button", { name: `Reject change ${i + 1}` })).toBeEnabled();
    // The ghost: moved (with a dimension callout), added or removed.
    const ghost = canvas.locator(`[data-testid=change-ghost][data-change="${c.id}"]`);
    const moved = c.delta.kind === "modified" && !!c.delta.move;
    await expect(ghost).toHaveAttribute("data-kind", moved ? "moved" : c.delta.kind);
    if (moved) await expect(ghost.getByTestId("change-callout")).toContainText(Math.hypot(...c.delta.move!).toFixed(1));
  }
});

test("accept and reject call the API and the model updates", async ({ page, request }) => {
  const { api, viewer, panel, canvas, toggle } = await openProposal(page, request);
  const [first] = CHANGE_SET.changes;
  await toggle.click();
  await expect(panel).toBeVisible();
  const before = await viewer.getAttribute("data-build");

  // Accept: the API is called with the change, the rebuild becomes the shown model.
  const accepted = page.waitForRequest((r) => r.method() === "POST" && r.url().endsWith("/changes"));
  await panel.getByRole("button", { name: "Accept change 1" }).click();
  expect((await accepted).postDataJSON()).toEqual({ accept: [first.id], reject: [] });
  const row = panel.locator(`li[data-change="${first.id}"]`);
  await expect(row).toHaveAttribute("data-status", "accepted");
  await expect(panel.getByRole("button", { name: "Accept change 1" })).toBeDisabled();
  const set = await (await request.get(`${api}/changes`)).json();
  expect(set.build).not.toBe(before);
  await expect(viewer).toHaveAttribute("data-build", set.build);
  await expect(viewer).toHaveAttribute("data-state", "ready");
  await expect(panel).toHaveAttribute("data-build", set.build);
  await expect(panel.getByTestId("change-checks")).toContainText(`${set.checks.pass} pass · ${set.checks.fail} fail`);
  // An accepted change has no ghost; its pin stays, marked accepted.
  await expect(canvas.locator(`[data-testid=change-ghost][data-change="${first.id}"]`)).toHaveCount(0);
  await expect(canvas.locator(`[data-testid=change-pin][data-change="${first.id}"]`)).toHaveAttribute("data-status", "accepted");

  // Reject it again: another rebuild, and the change is rejected.
  const rejected = page.waitForRequest((r) => r.method() === "POST" && r.url().endsWith("/changes"));
  await panel.getByRole("button", { name: "Reject change 1" }).click();
  expect((await rejected).postDataJSON()).toEqual({ accept: [], reject: [first.id] });
  await expect(row).toHaveAttribute("data-status", "rejected");
  const after = await (await request.get(`${api}/changes`)).json();
  expect(after.build).not.toBe(set.build);
  await expect(viewer).toHaveAttribute("data-build", after.build);
  expect(after.changes.find((c: Change) => c.id === first.id).status).toBe("rejected");
});
