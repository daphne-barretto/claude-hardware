import type { Page } from "@playwright/test";
import { BOX, boardlessSession, boardDemo, openBoardDemo, exampleSession, expect, getSession, openPage, openSession, send, settle, test } from "./live";
import { clickModel, MOVE } from "./states";
import { statSync } from "node:fs";

// End-to-end flows across the features that were built separately, on the real backend with a
// scripted Claude: the places where they collide. Each failure an in-flight branch owns is
// marked test.fail() with its owner, so it shows up as fixed (an unexpected pass) once the
// branch lands.

const canvas = (page: Page) => page.getByRole("region", { name: "Canvas" });
const chat = (page: Page) => page.getByRole("region", { name: "Chat" });
const builds = async (page: Page, id: string) => (await (await page.request.get(`/api/sessions/${id}/builds`)).json()) as { id: string; status: string }[];
const sessionId = (page: Page) => new URL(page.url()).pathname.split("/").pop()!;

test("opening a demo shows a session of your own with its design", async ({ page, monitor }) => {
  const demo = await boardDemo(page);
  await openPage(page, `/demo/${demo.id}`);
  await expect(page).toHaveURL(/\/session\/[\w-]+$/, { timeout: 30_000 });
  const viewer = page.getByTestId("viewer");
  await expect(viewer).toHaveAttribute("data-state", "ready", { timeout: 45_000 });
  // A session of your own takes messages: the composer, no read-only notice.
  await expect(page.getByTestId("sample-notice")).toHaveCount(0);
  await expect(page.getByRole("textbox", { name: "Message" })).toBeEditable();
  expect((await builds(page, sessionId(page))).length).toBeGreaterThan(0);
  expect(monitor.problems()).toEqual([]);
});

test("a board-less design from New: sent, built, checked and shown, with nothing about a board requested", async ({ page, monitor }) => {
  await openPage(page, "/new");
  await page.getByRole("textbox", { name: "Message" }).fill(BOX);
  await chat(page).getByRole("button", { name: "Send" }).click();
  await expect(page).toHaveURL(/\/session\/[\w-]+$/, { timeout: 30_000 });
  await expect(page.getByTestId("viewer")).toHaveAttribute("data-state", "ready", { timeout: 60_000 });
  // The box fails a team standard, so Claude asks whether to keep it: keep it, in the chat.
  if ((await settle(page, sessionId(page))) === "waiting_for_answer")
    await chat(page).getByRole("button", { name: /^Keep it / }).click();
  await expect.poll(() => settle(page, sessionId(page)), { timeout: 60_000 }).toBe("idle");
  await expect(chat(page).getByRole("button", { name: /^Build build-/ })).toBeVisible();
  // The chat names failing checks as people do, never by their IDs (fixed in this sweep).
  await expect(chat(page).getByText(/Checks still failing: [A-Z]/)).toBeVisible();
  await expect(chat(page).getByText(/Checks still failing: [a-z]+_[a-z_]+/)).toHaveCount(0);
  expect(monitor.problems().filter((p) => !/\/board$/.test(p))).toEqual([]);
});

test("a board-less design asks for no board", async ({ page, monitor }) => {
  await openPage(page, "/new");
  await page.getByRole("textbox", { name: "Message" }).fill(BOX);
  await chat(page).getByRole("button", { name: "Send" }).click();
  await expect(page.getByTestId("viewer")).toHaveAttribute("data-state", "ready", { timeout: 60_000 });
  await page.waitForTimeout(1_000);
  expect(monitor.http.filter((h) => h.url.endsWith("/board"))).toEqual([]);
});

for (const kind of ["board", "boardless"] as const) {
  test(`${kind}: a hole added with the modeling tools rebuilds, and Undo and Redo restore each design`, async ({ page, monitor }) => {
    const sid = kind === "board" ? await openBoardDemo(page) : await boardlessSession(page);
    await openSession(page, sid);
    const before = (await builds(page, sid)).length;
    await clickModel(page);
    await canvas(page).getByRole("toolbar", { name: "Modeling tools" }).getByRole("button", { name: "Hole", exact: true }).click();
    const panel = page.getByRole("region", { name: "Modeling" });
    await expect(panel.getByRole("button", { name: "Add hole" })).toBeEnabled({ timeout: 15_000 });
    await panel.getByRole("button", { name: "Add hole" }).click();
    await expect(panel.getByTestId("edit-notice")).toBeVisible({ timeout: 60_000 });
    await expect.poll(async () => (await builds(page, sid)).length, { timeout: 60_000 }).toBe(before + 1);
    const made = (await builds(page, sid)).at(-1)!;
    expect(made.status).toBe("ready");
    const undo = canvas(page).getByRole("toolbar", { name: "Modeling tools" }).getByRole("button", { name: "Undo", exact: true });
    await expect(undo).toBeEnabled({ timeout: 30_000 });
    await undo.click();
    await expect.poll(async () => (await getSession(page, sid)).latest_build_id, { timeout: 60_000 }).not.toBe(made.id);
    // Undo restores a build that works, never a failed one.
    const restored = (await builds(page, sid)).at(-1)!;
    expect(restored.status).toBe("ready");
    expect(monitor.problems().filter((p) => !/\/board$/.test(p))).toEqual([]);
  });
}

for (const kind of ["board", "boardless"] as const) {
  test(`${kind}: Export downloads the chosen files`, async ({ page, monitor }) => {
    const sid = kind === "board" ? await openBoardDemo(page) : await boardlessSession(page);
    await openSession(page, sid);
    await canvas(page).getByRole("button", { name: "Export", exact: true }).click();
    const panel = page.getByRole("region", { name: "Export" });
    await panel.getByRole("button", { name: /^Download \d+ files?$/ }).click();
    const files = panel.getByRole("list", { name: "Exported files" }).getByRole("link");
    await expect(files.first()).toBeVisible({ timeout: 60_000 });
    const download = page.waitForEvent("download");
    await files.first().click();
    const d = await download;
    const path = await d.path();
    expect(path).toBeTruthy();
    expect(statSync(path!).size).toBeGreaterThan(0);
    expect(monitor.problems().filter((p) => !/\/board$/.test(p))).toEqual([]);
  });
}

test("after a run, History shows the older build and Restore brings it back as a new build", async ({ page, monitor }) => {
  const sid = await exampleSession(page);
  await send(page, sid, MOVE);
  await openSession(page, sid);
  const count = (await builds(page, sid)).length;
  await canvas(page).getByRole("button", { name: "History", exact: true }).click();
  const history = page.getByRole("region", { name: "History" });
  const restore = history.getByRole("button", { name: /^Restore / }).first();
  await expect(restore).toBeVisible();
  await restore.click();
  await expect.poll(async () => (await builds(page, sid)).length, { timeout: 60_000 }).toBe(count + 1);
  expect((await builds(page, sid)).at(-1)!.status).toBe("ready");
  expect(monitor.problems()).toEqual([]);
});

test("a comment sent to Claude reaches the run with the comment", async ({ page, monitor }) => {
  const sid = await openBoardDemo(page);
  await openSession(page, sid);
  await canvas(page).getByRole("toolbar", { name: "Comment tools" }).getByRole("button", { name: "Comment", exact: true }).click();
  await clickModel(page, 0.45, 0.45);
  const draft = canvas(page).getByRole("dialog", { name: "New comment" });
  await draft.getByRole("textbox", { name: "Comment text" }).fill("Make this wall thicker");
  await draft.getByRole("button", { name: "Send to Claude" }).click();
  await expect(canvas(page).getByRole("dialog", { name: "Comment thread" })).toBeVisible();
  await expect.poll(async () => (await getSession(page, sid)).status, { timeout: 30_000 }).not.toBe("running");
  const messages = (await (await page.request.get(`/api/sessions/${sid}/messages`)).json()) as { role: string; comments?: unknown[] }[];
  expect(messages.some((m) => m.role === "user" && (m.comments ?? []).length > 0)).toBe(true);
  expect(monitor.problems()).toEqual([]);
});

test("phone: a session's canvas tools are reachable and the chat comes back", async ({ page, monitor }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const sid = await openBoardDemo(page);
  await openSession(page, sid);
  const exportButton = canvas(page).getByRole("button", { name: "Export", exact: true });
  await exportButton.scrollIntoViewIfNeeded();
  await exportButton.click();
  await expect(page.getByRole("region", { name: "Export" })).toBeVisible();
  await canvas(page).getByRole("button", { name: "Close canvas" }).click();
  await expect(page.getByRole("textbox", { name: "Message" })).toBeVisible();
  expect(monitor.problems()).toEqual([]);
});

test("phone: Show canvas can be tapped beside the standards picker, and a modeling tool opens", async ({ page, monitor }) => {
  // QA brief 18: at 390 px the chat header's standards picker (220 px, not shrinking) covered
  // Show canvas, so the canvas, and every modeling tool on it, couldn't be opened.
  await page.setViewportSize({ width: 390, height: 844 });
  const sid = await openBoardDemo(page);
  await openPage(page, `/session/${sid}`);
  const picker = chat(page).getByRole("button", { name: /^Constraint set/ });
  await expect(picker).toBeVisible();
  const show = chat(page).getByRole("button", { name: "Show canvas" });
  await expect(show).toBeVisible();
  const covered = await show.evaluate((el) => {
    const r = el.getBoundingClientRect();
    const top = document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2);
    return top && !el.contains(top) ? (top.closest("button")?.getAttribute("aria-label") ?? top.tagName) : null;
  });
  expect(covered, "what covers Show canvas").toBeNull();
  const box = (await picker.boundingBox())!;
  expect(box.x + box.width).toBeLessThanOrEqual(390);
  await show.click();
  await expect(page.getByTestId("viewer")).toBeVisible();
  await expect(page.getByTestId("viewer")).toHaveAttribute("data-state", "ready", { timeout: 90_000 });
  await canvas(page).getByRole("toolbar", { name: "Modeling tools" }).getByRole("button", { name: "Hole", exact: true }).click();
  await expect(page.getByRole("region", { name: "Modeling" })).toBeVisible();
  expect(monitor.problems()).toEqual([]);
});
