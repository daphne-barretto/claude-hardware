import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { parse } from "yaml";
import type { Page } from "@playwright/test";
import { boardlessSession, expect, getSession, openPage, openSession, settle, test } from "./live";

// Cases other agents found, on the real backend with the scripted Claude.

const root = resolve(__dirname, "../../..");
type Standard = { id: string; check: string; applies_to: string };
const standards = (parse(readFileSync(resolve(root, "standards.yaml"), "utf8")) as { standards: Standard[] }).standards;

test("a STEP file imports into a session without a board, and board-only checks are not applicable", async ({ page, monitor }) => {
  test.setTimeout(400_000);
  // A small STEP the app exported itself: a board-less box.
  const boxId = await boardlessSession(page);
  const { latest_build_id } = await getSession(page, boxId);
  const exported = await page.request.post(`/api/sessions/${boxId}/exports`, {
    data: { build_id: latest_build_id, formats: ["step"] },
    timeout: 180_000,
  });
  expect(exported.status(), await exported.text()).toBe(201);
  const file = ((await exported.json()) as { files: { format: string; url: string; name: string }[] }).files.find((f) => f.format === "step")!;
  const step = await (await page.request.get(file.url)).body();
  const created = await page.request.post("/api/sessions", { data: {} });
  const { id } = (await created.json()) as { id: string };
  const res = await page.request.post(`/api/sessions/${id}/imports`, {
    multipart: { file: { name: file.name, mimeType: "application/step", buffer: step } },
    timeout: 300_000,
  });
  expect(res.status(), await res.text()).toBe(201);
  const build = (await res.json()) as { id: string; board: unknown; checks_url: string };
  expect(build.board).toBeNull();
  const report = (await (await page.request.get(build.checks_url)).json()) as { results: { id: string; standard?: string | null; status: string }[] };
  const boardOnly = new Set(standards.filter((s) => s.applies_to === "board").map((s) => s.id));
  const results = report.results.filter((r) => r.standard && boardOnly.has(r.standard));
  expect(results.length).toBeGreaterThan(0);
  for (const r of results) expect(r.status, r.id).toBe("not_applicable");
  // The import shows on the canvas like any build.
  await openSession(page, id);
  expect(monitor.problems().filter((p) => !/\/board$/.test(p))).toEqual([]);
});

/**
 * Ten sessions created at once by one client, each sent its first message at once. `opened`:
 * the browser opened the app first, so it has its owner cookie; otherwise its first requests
 * go out without one.
 */
async function parallelCreate(page: Page, opened: boolean) {
  if (opened) await openPage(page, "/");
  const N = 10;
  const ids = await Promise.all(
    Array.from({ length: N }, async () => {
      const r = await page.request.post("/api/sessions", { data: {}, timeout: 120_000 });
      expect(r.status()).toBe(201);
      return ((await r.json()) as { id: string }).id;
    }),
  );
  expect(new Set(ids).size).toBe(N);
  // Every first message is accepted (no 404 for a session that was just made), all at once.
  const statuses = await Promise.all(
    ids.map(async (id) => (await page.request.post(`/api/sessions/${id}/messages`, { data: { text: "What can you make?" }, timeout: 120_000 })).status()),
  );
  expect(statuses).toEqual(Array(N).fill(202));
  for (const id of ids) {
    expect(await settle(page, id, 180_000)).not.toBe("running");
    expect((await getSession(page, id)).id).toBe(id);
  }
}

// Reproduced: the page's first API requests went out without an owner cookie, each got its own
// new owner, and the browser kept the last; sessions made meanwhile 404'd (fixed: src/proxy.ts).
// A client that calls the API directly must send back the cookie from its first response
// (backend/app/owners.py), so it makes one request before going parallel.
test("ten sessions created at once in a browser that opened the app each take their first message", async ({ page }) => {
  test.setTimeout(300_000);
  await parallelCreate(page, true);
});

test("ten browsers each create a session at once and send its first message", async ({ browser, baseURL }) => {
  test.setTimeout(300_000);
  const contexts = await Promise.all(
    Array.from({ length: 10 }, async () => {
      const c = await browser.newContext({ baseURL });
      await c.addCookies([{ name: "e2e-backend", value: "live", url: baseURL! }]);
      return c;
    }),
  );
  try {
    const results = await Promise.all(
      contexts.map(async (c) => {
        const created = await c.request.post("/api/sessions", { data: {}, timeout: 120_000 });
        const { id } = (await created.json()) as { id: string };
        const sent = await c.request.post(`/api/sessions/${id}/messages`, { data: { text: "What can you make?" }, timeout: 120_000 });
        return `${created.status()} ${sent.status()}${sent.ok() ? "" : ` ${await sent.text()}`}`;
      }),
    );
    expect(results).toEqual(Array(10).fill("201 202"));
  } finally {
    await Promise.all(contexts.map((c) => c.close()));
  }
});
