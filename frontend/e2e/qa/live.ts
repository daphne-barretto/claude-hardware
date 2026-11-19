import { mkdirSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";
import { test as base, expect, type Page, type Response } from "@playwright/test";

// The QA harness (`make qa`): the app on the real backend with a scripted
// Claude (backend/app/e2e_api.py), never the real model. Every page carries the cookie
// `e2e-backend=live`, so the dev server sends /api to that backend (next.config.ts).

export const LIVE_URL = process.env.QA_LIVE_URL ?? `http://127.0.0.1:${process.env.QA_LIVE_PORT ?? 8250}`;
export const QA_OUT = resolve(__dirname, "../../test-results/qa");
export const SHOTS = resolve(__dirname, "../../test-results/screenshots/qa");

/** What went wrong on the page while a test ran: console errors, page errors, failed requests. */
export type Monitor = {
  console: string[];
  http: { status: number; method: string; url: string }[];
  /** Statuses (or url patterns with statuses) the test expects, so they are not failures. */
  allow: (status: number, url?: RegExp) => void;
  problems: () => string[];
};

export const test = base.extend<{ monitor: Monitor }>({
  context: async ({ context, baseURL }, provide) => {
    await context.addCookies([{ name: "e2e-backend", value: "live", url: baseURL! }]);
    await context.grantPermissions(["clipboard-read", "clipboard-write"]).catch(() => {});
    await provide(context);
  },
  monitor: async ({ page }, provide) => {
    const consoleErrors: string[] = [];
    const http: Monitor["http"] = [];
    // The session API answers 404 for "none yet" on a session's change set and risk flags
    // (docs/architecture.md); the canvas asks for them after each build.
    const allowed: { status: number; url?: RegExp }[] = [{ status: 404, url: /^\/api\/sessions\/[^/]+\/(changes|risks)$/ }];
    page.on("pageerror", (err) => consoleErrors.push(`page error: ${err.message}`));
    page.on("console", (msg) => {
      if (msg.type() !== "error") return;
      // The browser's own line for a failed request: the request itself is checked below.
      if (/^Failed to load resource/.test(msg.text())) return;
      consoleErrors.push(`console error: ${msg.text()}`);
    });
    page.on("response", (r: Response) => {
      if (r.status() < 400) return;
      const url = new URL(r.url());
      http.push({ status: r.status(), method: r.request().method(), url: url.pathname + url.search });
    });
    await page.addInitScript(() => {
      // The Next.js dev tools badge is not part of the app.
      document.addEventListener("DOMContentLoaded", () => {
        const style = document.createElement("style");
        style.textContent = "nextjs-portal { display: none !important; }";
        document.head.appendChild(style);
      });
      window.addEventListener("unhandledrejection", (e) => {
        const r = e.reason as { message?: string } | undefined;
        console.error(`unhandled rejection: ${r?.message ?? String(e.reason)}`);
      });
    });
    const monitor: Monitor = {
      console: consoleErrors,
      http,
      allow: (status, url) => allowed.push({ status, url }),
      problems: () => [
        ...consoleErrors,
        ...http
          .filter((h) => !allowed.some((a) => a.status === h.status && (!a.url || a.url.test(h.url))))
          .map((h) => `HTTP ${h.status} ${h.method} ${h.url}`),
      ],
    };
    await provide(monitor);
  },
});
export { expect };

// ---------------------------------------------------------------------------------------
// The session API, as the page's browser (its owner cookie) sees it.

type Session = { id: string; status: string; latest_build_id: string | null; board: unknown };

async function api<T>(page: Page, method: "GET" | "POST", path: string, data?: unknown): Promise<T> {
  // Builds share one slot on the backend, so under parallel tests a request can wait its turn.
  const timeout = 120_000;
  const res = method === "GET" ? await page.request.get(path, { timeout }) : await page.request.post(path, { data: data ?? {}, timeout });
  expect(res.ok(), `${method} ${path}: ${res.status()} ${await res.text().catch(() => "")}`).toBe(true);
  return (await res.json()) as T;
}

export const getSession = (page: Page, id: string) => api<Session>(page, "GET", `/api/sessions/${id}`);

/** Waits until the session's run is over (idle, or waiting on a question). */
export async function settle(page: Page, id: string, timeout = 90_000) {
  let status = "";
  await expect
    .poll(async () => (status = (await getSession(page, id)).status), { timeout, intervals: [250, 500, 1000] })
    .not.toBe("running");
  return status;
}

export async function send(page: Page, id: string, text: string, extra: Record<string, unknown> = {}) {
  await api(page, "POST", `/api/sessions/${id}/messages`, { text, ...extra });
  return settle(page, id);
}

/** The board example (its board, the existing enclosure and a next revision). */
export async function exampleSession(page: Page) {
  const [example] = await api<{ id: string }[]>(page, "GET", "/api/examples");
  const s = await api<Session>(page, "POST", "/api/sessions", { example: example.id });
  await expect.poll(async () => (await getSession(page, s.id)).latest_build_id, { timeout: 60_000 }).not.toBeNull();
  return s.id;
}

/** The last demo whose board is set (the case updated for the new revision), as the session list names it: `id` is the demo's. */
export async function boardDemo(page: Page) {
  const sessions = await api<(Session & { demo?: string | null; title: string })[]>(page, "GET", "/api/sessions");
  const demos = sessions.filter((s) => s.demo && s.board);
  const last = demos[demos.length - 1];
  return { ...last, id: last.demo! };
}

/** A fresh copy of a board demo, this browser's own: the demo is reset first, so each call starts over. */
export async function openBoardDemo(page: Page, demoId?: string) {
  const id = demoId ?? (await boardDemo(page)).id;
  const reset = await page.request.delete(`/api/demos/${id}/session`);
  expect(reset.status(), `DELETE /api/demos/${id}/session`).toBe(204);
  const s = await api<Session>(page, "POST", `/api/demos/${id}/session`);
  return s.id;
}

/** A board-less design from scratch: an empty session, sent `text`, built and checked for real. */
export async function boardlessSession(page: Page, text = BOX) {
  const s = await api<Session>(page, "POST", "/api/sessions", {});
  // A design that fails a team standard ends with Claude asking whether to keep it: keep it.
  const status = await keepIfAsked(page, s.id, await send(page, s.id, text));
  expect(status).toBe("idle");
  expect((await getSession(page, s.id)).latest_build_id).not.toBeNull();
  return s.id;
}
export const BOX = "make a box 40 x 30 x 20 mm with 2 mm walls";

/** Answers "keep it" when a run ends asking whether to keep a design that fails a standard. */
export async function keepIfAsked(page: Page, id: string, status: string) {
  if (status !== "waiting_for_answer") return status;
  const q = await openQuestion(page, id);
  const keep = q?.options.findIndex((o) => o.id === "keep") ?? -1;
  return keep >= 0 ? answer(page, id, keep) : status;
}

type Question = { question_id: string; text: string; options: { id: string; label: string }[] };

/**
 * The open question of a session waiting on one: the last question its event stream resends.
 * The stream stays open, so it reads until a question has come and nothing more arrives.
 */
export async function openQuestion(page: Page, id: string): Promise<Question | null> {
  const cookie = (await page.context().cookies()).map((c) => `${c.name}=${c.value}`).join("; ");
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 10_000);
  let text = "";
  try {
    const res = await fetch(`${LIVE_URL}/api/sessions/${id}/events`, { headers: { cookie }, signal: ctrl.signal });
    const reader = res.body!.getReader();
    const last = () =>
      text
        .split("\n")
        .filter((l) => l.startsWith("data: ") && l.includes('"type":"question"'))
        .map((l) => JSON.parse(l.slice(6)) as Question)
        .at(-1) ?? null;
    for (;;) {
      const quiet = new Promise<"quiet">((r) => setTimeout(() => r("quiet"), last() ? 300 : 5_000));
      const next = await Promise.race([reader.read(), quiet]);
      if (next === "quiet" || next.done) break;
      text += Buffer.from(next.value).toString("utf8");
    }
    return last();
  } catch {
    return null;
  } finally {
    clearTimeout(timer);
    ctrl.abort();
  }
}

export async function answer(page: Page, id: string, optionIndex = 0) {
  const q = await openQuestion(page, id);
  expect(q, "an open question").not.toBeNull();
  await api(page, "POST", `/api/sessions/${id}/messages`, {
    text: "",
    answer: { question_id: q!.question_id, option_id: q!.options[optionIndex].id },
  });
  return settle(page, id);
}

// ---------------------------------------------------------------------------------------
// Pages

export async function openPage(page: Page, path: string) {
  await page.goto(path);
  await expect(page.locator("[data-shell-ready=true]")).toBeAttached({ timeout: 30_000 });
}

/** Opens a session and waits for its canvas (shown first on phone width) to be ready. */
export async function openSession(page: Page, id: string) {
  await openPage(page, `/session/${id}`);
  await showCanvas(page);
  const viewer = page.getByTestId("viewer");
  // Under load (other browsers building) the mesh can take a while to arrive.
  await expect(viewer).toHaveAttribute("data-state", "ready", { timeout: 90_000 });
  // On a phone the viewer is ready while its pane is hidden: the canvas must be the one shown.
  await expect(viewer).toBeVisible();
}

/**
 * At phone width the chat and the canvas take the screen one at a time: wait for Show canvas
 * (it comes with the session) and tap it. A wider screen shows the canvas beside the chat
 * unless it was hidden.
 */
export async function showCanvas(page: Page) {
  const show = page.getByRole("region", { name: "Chat" }).getByRole("button", { name: "Show canvas" });
  if ((page.viewportSize()?.width ?? 1280) < 768) await expect(show).toBeVisible({ timeout: 30_000 });
  if (await show.isVisible().catch(() => false)) await show.click();
}

export async function showChat(page: Page) {
  const close = page.getByRole("region", { name: "Canvas" }).getByRole("button", { name: "Close canvas" });
  const vw = page.viewportSize()?.width ?? 1280;
  if (vw < 768 && (await close.isVisible().catch(() => false))) await close.click();
}

// ---------------------------------------------------------------------------------------
// Findings: each test writes what it found, for the report (e2e/qa/report.ts).

export type Finding = {
  /** The QA state and viewport, such as `board.section@laptop`. */
  where: string;
  /** overlap, offscreen, hscroll, truncated, loading, focus, contrast, internal-text, console, http, dead, missing-inventory, ... */
  kind: string;
  detail: string;
  /** The in-flight branch that owns it, when one does (known.ts). */
  owner?: string;
  screenshot?: string;
};

export function writeFindings(file: string, findings: Finding[], extra: Record<string, unknown> = {}) {
  const dir = resolve(QA_OUT, "findings");
  mkdirSync(dir, { recursive: true });
  writeFileSync(resolve(dir, `${file.replace(/[^\w.@-]+/g, "_")}.json`), JSON.stringify({ findings, ...extra }, null, 2));
}

export async function shot(page: Page, name: string) {
  mkdirSync(SHOTS, { recursive: true });
  const file = `${name.replace(/[^\w.@-]+/g, "_")}.png`;
  await page.screenshot({ path: resolve(SHOTS, file) }).catch(() => {});
  return `frontend/test-results/screenshots/qa/${file}`;
}
