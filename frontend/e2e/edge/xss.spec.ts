import { expect, test, type Page } from "@playwright/test";
import { chatOf, live, newSession, open, settle, watchForXss, XSS } from "./helpers";

// No script runs from anything a user or Claude writes (docs/security.md): session titles,
// messages, Claude's markdown-looking output, questions, progress lines, errors and comments are
// all rendered as text. Each payload sets `window.__xss` or opens a dialog if it ever runs.
test.skip(!!process.env.E2E_BASE_URL, "the scripted backend runs only with the local dev servers");
test.beforeEach(async ({ context, baseURL }) => {
  test.setTimeout(240_000); // real builds take seconds each, more on a busy machine
  await live(context, baseURL);
});

const ALL = XSS.join(" ");

test("titles and messages render as text, in the chat and in Recents", async ({ page }) => {
  const watch = watchForXss(page);
  const session = await newSession(page.request, { title: `# ${ALL} **bold**` });
  await page.request.post(`/api/sessions/${session.id}/messages`, { data: { text: ALL } });
  await settle(page.request, session.id);
  await open(page, `/session/${session.id}`);
  await expect(chatOf(page).getByText(XSS[0], { exact: false }).first()).toBeVisible();
  await expect(page.getByRole("list", { name: "Recents" }).getByText(XSS[1], { exact: false }).first()).toBeVisible();
  await page.waitForTimeout(500);
  await watch.check();
});

/** Claude's side of a session, as the stream would carry it. */
async function streamClaude(page: Page, id: string, events: Record<string, unknown>[]) {
  // The page's own stream, which page.route sees: without a shared worker the tab connects
  // itself (src/lib/session-stream.ts) instead of through the browser's shared one.
  await page.addInitScript(() => Reflect.deleteProperty(window, "SharedWorker"));
  const body = events.map((e, i) => `id: ${i + 1}\nevent: ${e.type}\ndata: ${JSON.stringify(e)}\n\n`).join("");
  await page.route(`**/api/sessions/${id}/events**`, (route) =>
    route.fulfill({ status: 200, headers: { "content-type": "text/event-stream" }, body }),
  );
}

test("Claude's output, questions, progress lines and errors render as text", async ({ page }) => {
  const watch = watchForXss(page);
  const session = await newSession(page.request);
  const m = "msg-2";
  await streamClaude(page, session.id, [
    { type: "message_start", message_id: m, role: "assistant" },
    { type: "progress", message_id: m, text: `Building ${XSS[2]}`, round: 1, rounds: 4 },
    { type: "text", message_id: m, delta: `## Heading\n\n${ALL}\n\n\`\`\`html\n<script>window.__xss='code'</script>\n\`\`\`` },
    {
      type: "question",
      message_id: m,
      question_id: "q-1",
      text: `Which? ${XSS[0]}`,
      options: [
        { id: "a", label: XSS[1], description: XSS[3] },
        { id: "b", label: "Plain", description: XSS[5] },
      ],
    },
    { type: "error", message_id: m, message: `Failed: ${XSS[0]}`, retryable: true },
    { type: "message_end", message_id: m },
  ]);
  await open(page, `/session/${session.id}`);
  await expect(chatOf(page).getByText("## Heading", { exact: false }).first()).toBeVisible({ timeout: 30_000 });
  await page.waitForTimeout(500);
  await watch.check();
});

test("comments and replies render as text on the canvas", async ({ page }) => {
  const watch = watchForXss(page);
  const examples = (await (await page.request.get("/api/examples")).json()) as { id: string }[];
  const session = await newSession(page.request, { example: examples[0].id });
  await expect.poll(async () => (await (await page.request.get(`/api/sessions/${session.id}/builds`)).json()).length, { timeout: 60_000 }).toBeGreaterThan(0);
  const build = ((await (await page.request.get(`/api/sessions/${session.id}/builds`)).json()) as { id: string }[])[0];
  const { FACE_IDS } = await import("./helpers");
  const comment = await page.request.post(`/api/sessions/${session.id}/comments`, {
    data: { build_id: build.id, face_id: FACE_IDS[0], point: [0, 0, 0], text: ALL },
  });
  expect(comment.status()).toBe(201);
  const { id } = (await comment.json()) as { id: string };
  await page.request.post(`/api/sessions/${session.id}/comments/${id}/replies`, { data: { text: XSS.reverse().join(" ") } });
  await page.setViewportSize({ width: 1440, height: 900 });
  await open(page, `/session/${session.id}`);
  const canvas = page.getByRole("region", { name: "Canvas" });
  await expect(page.getByTestId("viewer")).toHaveAttribute("data-state", "ready", { timeout: 30_000 });
  await canvas.getByRole("toolbar", { name: "Comment tools" }).getByRole("button", { name: "Comments" }).click();
  await expect(canvas.getByText("window.__xss", { exact: false }).first()).toBeVisible();
  await page.waitForTimeout(500);
  await watch.check();
});
