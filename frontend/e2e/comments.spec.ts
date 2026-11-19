import { readFileSync } from "node:fs";
import path from "node:path";
import { expect, test, type APIRequestContext, type Locator, type Page } from "@playwright/test";
import { answerOpenQuestion } from "./inventory/ui-inventory";

// Comments pinned to geometry. The mock session API tests pin, send, reply, accept, Undo
// and Resolve, and batching; the real backend (backend/app/e2e_api.py, real builds with a
// scripted Claude) tests that a pin follows its face through a rebuild that moves it. Faces,
// features and changes come from the fixtures, the real API and demos/b-parasite/CHANGES.md.

type Extras = { part: string; faces: { id: string; feature: string }[] };
type Comment = {
  id: string;
  face_id: string;
  feature?: string | null;
  location?: { point: [number, number, number]; faces?: string[] } | null;
  resolved: boolean;
};
type Change = { id: string; title: string; status: string; comment?: string | null; delta: { feature?: string | null } };
type Build = { id: string; mesh_url: string };

const repo = (...parts: string[]) => path.resolve(__dirname, "../..", ...parts);
const fixture = (name: string) => JSON.parse(readFileSync(repo("fixtures", name), "utf8"));
const MESH = fixture("enclosure.gltf") as { nodes: { extras?: Extras }[] };
const FACE_IDS = new Set(MESH.nodes.flatMap((n) => n.extras?.faces?.map((f) => f.id) ?? []));
const SESSION = fixture("session.json") as { prompt: string };

const get = async <T>(request: APIRequestContext, url: string) => {
  const res = await request.get(url);
  expect(res.ok(), `${url}: ${res.status()}`).toBe(true);
  return (await res.json()) as T;
};

/** A mock session whose recorded run has finished, its question answered, open on the canvas. */
async function openMock(page: Page) {
  const session = (await (await page.request.post("/api/sessions", { data: {} })).json()) as { id: string };
  const api = `/api/sessions/${session.id}`;
  await page.request.post(`${api}/messages`, { data: { text: SESSION.prompt } });
  await expect
    .poll(async () => (await get<{ status: string }>(page.request, api)).status, { timeout: 30_000 })
    .not.toBe("running");
  await answerOpenQuestion(page, api);
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto(`/session/${session.id}`);
  await expect(page.locator("[data-shell-ready=true]")).toBeAttached({ timeout: 30_000 });
  const viewer = page.getByTestId("viewer");
  await expect(viewer).toHaveAttribute("data-state", "ready", { timeout: 30_000 });
  const canvas = page.getByRole("region", { name: "Canvas" });
  return { api, viewer, canvas, chat: page.getByRole("region", { name: "Chat" }) };
}

/** Pins a comment with the comment tool where the click lands, `at` as fractions of the model's on-screen box. */
async function pin(page: Page, viewer: Locator, canvas: Locator, text: string, at: [number, number], send = false) {
  const tool = canvas.getByRole("toolbar", { name: "Comment tools" }).getByRole("button", { name: "Comment", exact: true });
  if ((await tool.getAttribute("aria-pressed")) !== "true") await tool.click();
  const box = (await viewer.boundingBox())!;
  const [x, y, w, h] = ((await viewer.getAttribute("data-model-rect")) ?? "").split(",").map(Number);
  await page.mouse.click(box.x + x + w * at[0], box.y + y + h * at[1]);
  const draft = canvas.getByRole("dialog", { name: "New comment" });
  await draft.getByRole("textbox", { name: "Comment text" }).fill(text);
  const created = page.waitForResponse((r) => r.request().method() === "POST" && /\/comments$/.test(r.url()));
  await draft.getByRole("button", { name: send ? "Send to Claude" : "Comment", exact: true }).click();
  return (await (await created).json()) as Comment;
}

const messagePosts = (page: Page) => {
  const bodies: { comments?: string[]; text: string }[] = [];
  page.on("request", (r) => {
    if (r.method() === "POST" && /\/messages$/.test(r.url())) bodies.push(r.postDataJSON());
  });
  return bodies;
};

test.describe("on the mock API", () => {
  test.skip(!!process.env.E2E_BASE_URL, "the mock API runs only with the local dev servers");

  test("pin a comment, send it to Claude, accept and undo its change, and resolve it", async ({ page }) => {
    const { api, viewer, canvas, chat } = await openMock(page);
    await chat.getByRole("radio", { name: "Manual" }).click();
    const replies = await chat.getByTestId("assistant-message").count();
    const posts = messagePosts(page);

    // Pin: a note on the face under the click. It starts nothing.
    const comment = await pin(page, viewer, canvas, "Can we add a light pipe here?", [0.6, 0.5]);
    expect(FACE_IDS.has(comment.face_id), `${comment.face_id} is a face of the fixture mesh`).toBe(true);
    const pinEl = canvas.locator(`[data-testid=comment-pin][data-comment="${comment.id}"]`);
    await expect(pinEl).toBeVisible();
    await expect(pinEl).toHaveAttribute("data-face", comment.face_id);
    await expect(pinEl).toHaveAttribute("data-state", "note");
    const thread = canvas.getByRole("dialog", { name: "Comment thread" });
    await expect(thread).toHaveAttribute("data-comment", comment.id);
    expect(posts).toEqual([]);
    expect((await get<{ status: string }>(page.request, api)).status).toBe("idle");

    // Send: one run; Claude's reply is in the thread, not in the chat.
    await thread.getByRole("button", { name: "Send to Claude" }).click();
    await expect(thread.getByTestId("comment-reply-claude")).toHaveCount(1, { timeout: 30_000 });
    expect(posts.map((b) => b.comments)).toEqual([[comment.id]]);
    await expect(thread).toHaveAttribute("data-state", "answered");
    await expect(chat.getByTestId("assistant-message")).toHaveCount(replies);

    // The change made for it is a tracked change linked to it: accept, then Undo.
    const set = await get<{ changes: Change[] }>(page.request, `${api}/changes`);
    const linked = set.changes.filter((c) => c.comment === comment.id);
    expect(linked).toHaveLength(1);
    const change = thread.locator(`[data-testid=comment-change][data-change="${linked[0].id}"]`);
    await expect(change).toHaveAttribute("data-status", "proposed");
    await change.getByRole("button", { name: `Accept: ${linked[0].title}` }).click();
    await expect(change).toHaveAttribute("data-status", "accepted");
    await thread.getByRole("button", { name: "Undo" }).click();
    await expect(change).toHaveAttribute("data-status", "rejected");

    // Resolve hides the pin and keeps the thread in the comments list.
    await thread.getByRole("button", { name: "Resolve" }).click();
    await expect(pinEl).toHaveCount(0);
    await canvas.getByRole("toolbar", { name: "Comment tools" }).getByRole("button", { name: "Comments" }).click();
    const row = canvas.getByRole("region", { name: "Comments list" }).locator(`li[data-comment="${comment.id}"]`);
    await expect(row).toHaveAttribute("data-state", "resolved");
    expect((await get<Comment[]>(page.request, `${api}/comments`)).map((c) => [c.id, c.resolved])).toEqual([[comment.id, true]]);
  });

  test("three comments sent together make one run with a reply and a change each", async ({ page }) => {
    const { api, viewer, canvas, chat } = await openMock(page);
    const replies = await chat.getByTestId("assistant-message").count();
    const comments = [];
    for (const [n, at] of ([[1, [0.35, 0.45]], [2, [0.6, 0.5]], [3, [0.75, 0.7]]] as const)) {
      comments.push(await pin(page, viewer, canvas, `Request ${n}`, [at[0], at[1]]));
      await canvas.getByRole("dialog", { name: "Comment thread" }).getByRole("button", { name: "Close thread" }).click();
    }
    const posts = messagePosts(page);
    await canvas.getByRole("toolbar", { name: "Comment tools" }).getByRole("button", { name: "Comments" }).click();
    await canvas.getByRole("region", { name: "Comments list" }).getByRole("button", { name: "Send 3 to Claude" }).click();
    await expect.poll(() => posts.length).toBe(1);
    expect(posts[0].comments).toEqual(comments.map((c) => c.id));

    await expect
      .poll(async () => (await get<{ status: string }>(page.request, api)).status, { timeout: 30_000 })
      .toBe("idle");
    const set = await get<{ changes: Change[] }>(page.request, `${api}/changes`);
    expect(set.changes.map((c) => c.comment).sort()).toEqual(comments.map((c) => c.id).sort());
    for (const c of comments) {
      // Each thread, opened from the comments list.
      const list = canvas.getByRole("toolbar", { name: "Comment tools" }).getByRole("button", { name: "Comments" });
      if ((await list.getAttribute("aria-expanded")) !== "true") await list.click();
      await canvas.getByRole("region", { name: "Comments list" }).locator(`li[data-comment="${c.id}"] button`).click();
      const thread = canvas.getByRole("dialog", { name: "Comment thread" });
      await expect(thread).toHaveAttribute("data-comment", c.id);
      await expect(thread.getByTestId("comment-reply-claude")).toHaveCount(1);
      const mine = set.changes.find((x) => x.comment === c.id)!;
      await expect(thread.locator(`[data-testid=comment-change]`)).toHaveAttribute("data-change", mine.id);
    }
    await expect(chat.getByTestId("assistant-message")).toHaveCount(replies);
  });
});

// --- The real backend: a rebuild that moves the face ------------------------------------------

const CHANGES = readFileSync(repo("demos/b-parasite/CHANGES.md"), "utf8");
// Components whose opening only moved (not turned): every face of the opening moves with it.
const MOVED = CHANGES.split("\n")
  .filter((l) => l.startsWith("| `"))
  .map((l) => l.split("|").map((c) => c.trim()))
  .filter((cells) => cells[4] === "moved")
  .map((cells) => cells[1].replaceAll("`", ""));
// The scripted Claude moves the openings CHANGES.md lists as moved (backend/app/e2e_api.py).
const MOVE_OPENINGS = "Move the openings";

test.describe("on the real backend", () => {
  test.skip(!!process.env.E2E_BASE_URL, "the scripted backend runs only with the local dev servers");
  test.beforeEach(async ({ context, baseURL }) => {
    await context.addCookies([{ name: "e2e-backend", value: "live", url: baseURL! }]);
  });

  test("a pinned comment stays on its face after a rebuild that moves it", async ({ page }) => {
    test.setTimeout(150_000);
    expect(MOVED.length).toBeGreaterThan(0);
    const session = (await (await page.request.post("/api/sessions", { data: {} })).json()) as { id: string };
    const api = `/api/sessions/${session.id}`;
    let builds: Build[] = [];
    await expect
      .poll(async () => (builds = await get<Build[]>(page.request, `${api}/builds`)).length, { timeout: 60_000 })
      .toBeGreaterThan(0);
    const build = builds[0];
    const mesh = await get<{ nodes: { extras?: Extras }[] }>(page.request, build.mesh_url);
    const feature = `opening_${MOVED[0]}`;
    const face = mesh.nodes.flatMap((n) => n.extras?.faces ?? []).find((f) => f.feature === feature);
    expect(face, `a face of ${feature}`).toBeTruthy();

    // Pinned to that face (the API puts the point on it).
    const made = await page.request.post(`${api}/comments`, {
      data: { build_id: build.id, face_id: face!.id, point: [0, 0, 0], text: "Light pipe here?" },
    });
    expect(made.status()).toBe(201);
    const first = (await made.json()) as Comment;
    const r = await page.request.post(`${api}/comments`, {
      data: { build_id: build.id, face_id: face!.id, point: first.location!.point, text: "Light pipe here?" },
    });
    const comment = (await r.json()) as Comment;
    await page.request.patch(`${api}/comments/${first.id}`, { data: { resolved: true } });
    expect(comment.location!.faces).toEqual([face!.id]);

    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto(`/session/${session.id}`);
    const viewer = page.getByTestId("viewer");
    await expect(viewer).toHaveAttribute("data-state", "ready", { timeout: 30_000 });
    const pinEl = page.locator(`[data-testid=comment-pin][data-comment="${comment.id}"]`);
    await expect(pinEl).toHaveAttribute("data-face", face!.id);

    // Claude moves the openings: the model rebuilds.
    expect((await page.request.post(`${api}/messages`, { data: { text: MOVE_OPENINGS } })).ok()).toBe(true);
    let rebuilt: Build | undefined;
    await expect
      .poll(
        async () => {
          const all = await get<Build[]>(page.request, `${api}/builds`);
          rebuilt = (await get<{ status: string }>(page.request, api)).status === "idle" ? all.at(-1) : undefined;
          return rebuilt?.id ?? build.id;
        },
        { timeout: 90_000 },
      )
      .not.toBe(build.id);
    await expect(viewer).toHaveAttribute("data-build", rebuilt!.id, { timeout: 30_000 });
    await expect(viewer).toHaveAttribute("data-state", "ready", { timeout: 30_000 });

    // The opening moved; the comment is still on its face (found again by its stable ID) on the
    // new build. (Where the pin goes when the face itself moves is checked on real tessellated
    // geometry in backend/tests/test_comments.py: the mock session API serves build meshes from
    // the fixtures.)
    const diff = await get<{ features: { tag: string; change: string }[] }>(
      page.request,
      `${api}/builds/${rebuilt!.id}/diff?against=${build.id}`,
    );
    expect(diff.features.find((f) => f.tag === feature)?.change).toMatch(/moved/);
    const listed = await get<Comment[]>(page.request, `${api}/comments?build_id=${rebuilt!.id}`);
    const after = listed.find((c) => c.id === comment.id)!;
    expect(after.location!.faces).toEqual([face!.id]);
    await expect(pinEl).toBeVisible();
    await expect(pinEl).toHaveAttribute("data-face", face!.id);
  });
});
