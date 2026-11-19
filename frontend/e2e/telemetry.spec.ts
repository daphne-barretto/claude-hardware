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
const PREVIOUS = fixture<{ id: string; board: { revision: string } }>("previous-build.json");
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

test("choosing a mode is reported to the session's telemetry", async ({ page, request }) => {
  const id = await openSession(page, request);
  const mode = page.getByRole("region", { name: "Chat" }).getByRole("radiogroup", { name: "Mode" });
  const current = await mode.getByRole("radio", { checked: true }).textContent();
  const other = mode.getByRole("radio", { checked: false }).first();
  const value = ((await other.textContent()) ?? "").trim().toLowerCase();
  expect(value).not.toBe((current ?? "").trim().toLowerCase());
  const sent = reported(page, id, (b) => b.kind === "choice" && b.setting === "mode");
  await other.click();
  expect((await sent).postDataJSON()).toEqual({ kind: "choice", setting: "mode", value });
  const response = await (await sent).response();
  expect(response?.status()).toBe(204);
});

test("picking an analysis map is reported as a view", async ({ page, request }) => {
  const session = await openExample(request);
  const id: string = session.id;
  await request.post(`/api/sessions/${id}/messages`, { data: { text: "Update the enclosure." } });
  await expect
    .poll(async () => (await (await request.get(`/api/sessions/${id}`)).json()).status, { timeout: 30_000 })
    .not.toBe("running");
  await page.goto(`/session/${id}`);
  await expect(page.getByTestId("viewer")).toHaveAttribute("data-state", "ready", { timeout: 30_000 });
  const canvas = page.getByRole("region", { name: "Canvas" });
  await canvas.getByRole("button", { name: "Analysis maps" }).click();
  const item = canvas.getByRole("menu", { name: "Analysis maps" }).getByRole("menuitemradio").nth(1);
  const sent = reported(page, id, (b) => b.kind === "view" && b.view === "map");
  await item.click();
  const body = (await sent).postDataJSON();
  expect(typeof body.value).toBe("string");
  await expect(page.getByTestId("viewer")).toHaveAttribute("data-map", body.value);
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

test("opening compare and history are reported as views", async ({ page, request }) => {
  const { id, canvas } = await openFinished(page, request, "builds");

  const compared = reported(page, id, (b) => b.kind === "view" && b.view === "compare");
  const compare = canvas.getByRole("button", { name: `Compare with v${PREVIOUS.board.revision}` });
  await compare.click();
  expect((await compared).postDataJSON()).toEqual({ kind: "view", view: "compare", value: PREVIOUS.id });
  expect((await (await compared).response())?.status()).toBe(204);
  await compare.click(); // closing compare reports nothing more

  const opened = reported(page, id, (b) => b.kind === "view" && b.view === "history");
  await canvas.getByRole("button", { name: "History" }).click();
  expect((await opened).postDataJSON()).toEqual({ kind: "view", view: "history" });
  expect((await (await opened).response())?.status()).toBe(204);
  await expect(page.getByRole("region", { name: "History" })).toBeVisible();
});
