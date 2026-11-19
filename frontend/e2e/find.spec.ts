import { readFileSync } from "node:fs";
import path from "node:path";
import { expect, test, type APIRequestContext, type Locator, type Page } from "@playwright/test";
import { openExample } from "./fixtures/sessions";

// Find in model against the mock session API (docs/architecture.md). The queries are
// words, not scenario values; the expected faces come from the API and the fixtures.
test.skip(!!process.env.E2E_BASE_URL, "the mock API runs only with the local dev servers");

const read = (name: string) => JSON.parse(readFileSync(path.join(__dirname, "../../fixtures", name), "utf8"));
const SESSION = read("session.json") as { prompt: string };
const QUESTION = readFileSync(path.join(__dirname, "../../fixtures/session-events.jsonl"), "utf8")
  .split("\n")
  .filter(Boolean)
  .map((line) => JSON.parse(line).event as { type: string; question_id?: string; options?: { id: string }[] })
  .find((e) => e.type === "question")!;
type AnalysisMap = { flagged: { kind: string; location: { faces: string[] } }[] };
const FLAGGED = (read("build.json") as { maps: string[] }).maps.flatMap((m) => (read(`maps/${m}.json`) as AnalysisMap).flagged);
type FindResult = {
  status: string;
  understood: string;
  question?: string | null;
  matches: { face: string; part: string; feature: string }[];
};

// `answered`: answer Claude's question first, since the composer sends nothing else while it is open.
async function openCanvas(page: Page, request: APIRequestContext, { answered = false } = {}) {
  const session = await openExample(request);
  await request.post(`/api/sessions/${session.id}/messages`, { data: { text: SESSION.prompt } });
  await expect
    .poll(async () => (await (await request.get(`/api/sessions/${session.id}/builds`)).json()).length, { timeout: 30_000 })
    .toBeGreaterThan(0);
  const [build] = await (await request.get(`/api/sessions/${session.id}/builds`)).json();
  if (answered) {
    const answer = { question_id: QUESTION.question_id, option_id: QUESTION.options![0].id };
    await expect.poll(async () => (await request.post(`/api/sessions/${session.id}/messages`, { data: { answer } })).status(), { timeout: 30_000 }).toBeLessThan(300);
    await expect
      .poll(async () => (await (await request.get(`/api/sessions/${session.id}`)).json()).status, { timeout: 30_000 })
      .toBe("idle");
  }
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto(`/session/${session.id}`);
  await expect(page.locator("[data-shell-ready=true]")).toBeAttached({ timeout: 30_000 });
  const viewer = page.getByTestId("viewer");
  await expect(viewer).toHaveAttribute("data-state", "ready", { timeout: 30_000 });
  const find = async (query: string): Promise<FindResult> =>
    (await request.post(`/api/sessions/${session.id}/builds/${build.id}/find`, { data: { query } })).json();
  return {
    viewer,
    find,
    chat: page.getByRole("region", { name: "Chat" }),
    parts: page.getByRole("region", { name: "Parts" }),
  };
}

const selected = async (viewer: Locator) => ((await viewer.getAttribute("data-selected")) ?? "").split(",").filter(Boolean);

async function search(parts: Locator, query: string) {
  const box = parts.getByRole("textbox", { name: "Find in model" });
  await box.fill(query);
  await box.press("Enter");
}

test("an analysis-value search highlights and lists the matching faces", async ({ page, request }) => {
  const { viewer, parts, find } = await openCanvas(page, request);
  const expected = await find("flagged");
  expect(expected.status).toBe("found");
  // The spots the analysis flagged are among the matches.
  const flagged = FLAGGED.flatMap((s) => s.location.faces);
  expect(flagged.length).toBeGreaterThan(0);
  expect(expected.matches.map((m) => m.face)).toEqual(expect.arrayContaining(flagged));

  await search(parts, "flagged");
  const result = parts.getByTestId("find-result");
  await expect(result).toHaveAttribute("data-status", "found");
  await expect(parts.getByTestId("find-understood")).toContainText(expected.understood);
  await expect.poll(() => selected(viewer)).toEqual(expected.matches.map((m) => m.face));
  const features = [...new Set(expected.matches.map((m) => m.feature))];
  const items = parts.getByRole("list", { name: "Matches" }).getByRole("listitem");
  await expect(items).toHaveCount(features.length);
  for (const feature of features) await expect(parts.getByRole("list", { name: "Matches" }).locator(`li[data-feature="${feature}"]`)).toBeVisible();
});

test("matches are sent to chat as the selection", async ({ page, request }) => {
  const { viewer, parts, chat, find } = await openCanvas(page, request, { answered: true });
  const expected = await find("flagged");
  await search(parts, "flagged");
  await expect(parts.getByTestId("find-result")).toHaveAttribute("data-status", "found");
  // Highlighted, not yet in the chat.
  await expect(chat.getByTestId("selection-chip")).toHaveCount(0);

  await parts.getByRole("button", { name: "Send to chat" }).click();
  const chip = chat.getByTestId("selection-chip");
  await expect(chip).toContainText(expected.understood);

  const sent = page.waitForRequest((r) => r.method() === "POST" && /\/api\/sessions\/[^/]+\/messages$/.test(r.url()));
  await chat.getByRole("textbox", { name: "Message" }).fill("Why are these flagged?");
  await chat.getByRole("button", { name: "Send" }).click();
  const body = (await sent).postDataJSON();
  expect(body.selection.faces).toEqual(expected.matches.map((m) => m.face));
  expect(body.selection.edges).toEqual([]);
  await expect.poll(() => selected(viewer)).toEqual([]);
});

test("a search replaces a selection that is waiting in the chat", async ({ page, request }) => {
  const { viewer, parts, chat, find } = await openCanvas(page, request);
  const expected = await find("flagged");
  const other = (await find("all faces")).status; // unclear: nothing to search for
  expect(other).toBe("unclear");
  await parts.getByRole("button", { name: /^Select / }).first().click();
  await expect(chat.getByTestId("selection-chip")).toHaveCount(1);
  await search(parts, "flagged");
  await expect.poll(() => selected(viewer)).toEqual(expected.matches.map((m) => m.face));
  await expect(chat.getByTestId("selection-chip")).toHaveCount(0);
});

test("an unknown query says what was understood and asks", async ({ page, request }) => {
  const { viewer, parts, find } = await openCanvas(page, request);
  const expected = await find("purple widgets");
  expect(expected.status).toBe("unclear");
  await search(parts, "purple widgets");
  await expect(parts.getByTestId("find-result")).toHaveAttribute("data-status", "unclear");
  await expect(parts.getByTestId("find-understood")).toContainText(expected.understood);
  const question = parts.getByTestId("find-question");
  await expect(question).toHaveText(expected.question!);
  await expect(question).toContainText("“purple”");
  await expect(parts.getByRole("button", { name: "Send to chat" })).toHaveCount(0);
  expect(await selected(viewer)).toEqual([]);
});
