import { readFileSync } from "node:fs";
import path from "node:path";
import { expect, test, type APIRequestContext, type Page, type Request } from "@playwright/test";
import { openExample } from "./fixtures/sessions";

// Telemetry against the mock session API (docs/architecture.md): a session shows that it is
// recorded, and the actions only the frontend sees are reported to the session's log.
test.skip(!!process.env.E2E_BASE_URL, "the mock API runs only with the local dev servers");

// Scenario values come from the fixture files, never from here.
const fixture = <T>(name: string) => JSON.parse(readFileSync(path.join(__dirname, "../../fixtures", name), "utf8")) as T;
const SESSION = fixture<{ prompt: string }>("session.json");
const CHANGES = fixture<{ changes: { id: string }[] }>("change-set.json").changes;
const QUESTION = readFileSync(path.join(__dirname, "../../fixtures/session-events.jsonl"), "utf8")
  .split("\n")
  .filter(Boolean)
  .map((line) => JSON.parse(line).event)
  .find((e) => e.type === "question");

async function openSession(page: Page, request: APIRequestContext): Promise<string> {
  const session = await openExample(request);
  await page.goto(`/session/${session.id}`);
  await expect(page.locator("[data-shell-ready=true]")).toBeAttached({ timeout: 30_000 });
  return session.id;
}

function reported(page: Page, id: string, check: (body: Record<string, unknown>) => boolean): Promise<Request> {
  return page.waitForRequest(
    (r) => r.method() === "POST" && r.url().endsWith(`/api/sessions/${id}/telemetry`) && check(r.postDataJSON()),
  );
}

test("no recording notice is shown under the composer", async ({ page, request }) => {
  await openSession(page, request);
  await expect(page.getByTestId("recording-notice")).toHaveCount(0);
});

/** A mock session whose recorded run has finished (builds on both revisions), opened on the canvas. */
async function openFinished(page: Page, request: APIRequestContext, until: "builds" | "changes") {
  const session = await (await request.post("/api/sessions", { data: {} })).json();
  const api = `/api/sessions/${session.id}`;
  await request.post(`${api}/messages`, { data: { text: SESSION.prompt } });
  if (until === "changes") {
    await expect
      .poll(async () => (await (await request.get(api)).json()).status, { timeout: 30_000 })
      .toBe("waiting_for_answer");
    await request.post(`${api}/messages`, {
      data: { text: "", answer: { question_id: QUESTION.question_id, option_id: QUESTION.options[0].id } },
    });
    await expect.poll(async () => (await request.get(`${api}/changes`)).status(), { timeout: 30_000 }).toBe(200);
  } else {
    await expect
      .poll(async () => (await (await request.get(`${api}/builds`)).json()).length, { timeout: 30_000 })
      .toBe(2);
  }
  await page.goto(`/session/${session.id}`);
  await expect(page.getByTestId("viewer")).toHaveAttribute("data-state", "ready", { timeout: 30_000 });
  return { id: session.id as string, canvas: page.getByRole("region", { name: "Canvas" }) };
}

test("accepting and rejecting a change are reported as decisions", async ({ page, request }) => {
  const { id, canvas } = await openFinished(page, request, "changes");
  const [first] = CHANGES;
  await canvas.getByRole("button", { name: "Proposed changes", exact: true }).click();
  const panel = canvas.getByRole("region", { name: "Proposed changes" });

  const accepted = reported(page, id, (b) => b.kind === "change_decision" && b.decision === "accept");
  await panel.getByRole("button", { name: "Accept change 1" }).click();
  expect((await accepted).postDataJSON()).toEqual({ kind: "change_decision", change_id: first.id, decision: "accept" });
  expect((await (await accepted).response())?.status()).toBe(204);
  await expect(panel.locator(`li[data-change="${first.id}"]`)).toHaveAttribute("data-status", "accepted");

  const rejected = reported(page, id, (b) => b.kind === "change_decision" && b.decision === "reject");
  await panel.getByRole("button", { name: "Reject change 1" }).click();
  expect((await rejected).postDataJSON()).toEqual({ kind: "change_decision", change_id: first.id, decision: "reject" });
  expect((await (await rejected).response())?.status()).toBe(204);
});
