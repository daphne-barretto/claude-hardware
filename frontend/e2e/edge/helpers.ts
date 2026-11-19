import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { expect, type APIRequestContext, type BrowserContext, type Page } from "@playwright/test";

// Shared by the edge-case specs (docs/security.md, `make edge`). They run against the real
// backend with a scripted Claude (backend/app/e2e_api.py): the cookie `e2e-backend=live` sends
// a context's /api requests there (playwright.config.ts). Never the real model.

export const repo = (...parts: string[]) => resolve(__dirname, "../../..", ...parts);
const fixture = (name: string) => JSON.parse(readFileSync(repo("fixtures", name), "utf8"));
type Extras = { part: string; faces: { id: string; feature: string }[] };
export const FACE_IDS: string[] = (fixture("enclosure.gltf") as { nodes: { extras?: Extras }[] }).nodes.flatMap(
  (n) => n.extras?.faces?.map((f) => f.id) ?? [],
);

/** Payloads that run `window.__xss = …` (or open a dialog) if anything renders them as HTML. */
export const XSS = [
  `<img src=x onerror="window.__xss='img'">`,
  `<script>window.__xss='script'</script>`,
  `<svg onload="window.__xss='svg'"></svg>`,
  `"><iframe src="javascript:window.parent.__xss='iframe'"></iframe>`,
  `[click](javascript:window.__xss='md-link') ![x](x" onerror="window.__xss='md-img')`,
  `<a href="javascript:window.__xss='a'">link</a>`,
];

export async function live(context: BrowserContext, baseURL: string | undefined) {
  await context.addCookies([{ name: "e2e-backend", value: "live", url: baseURL! }]);
}

/** Watches a page for anything an injected payload would do. */
export function watchForXss(page: Page) {
  const fired: string[] = [];
  page.on("dialog", async (d) => {
    fired.push(`dialog: ${d.message()}`);
    await d.dismiss();
  });
  page.on("pageerror", (e) => fired.push(`pageerror: ${e.message}`));
  return {
    fired,
    async check() {
      const flag = await page.evaluate(() => (window as unknown as { __xss?: string }).__xss);
      if (flag) fired.push(`window.__xss = ${flag}`);
      expect(await page.locator('img[src="x"], iframe[src^="javascript:"], a[href^="javascript:"]').count()).toBe(0);
      expect(fired).toEqual([]);
    },
  };
}

export async function open(page: Page, path: string) {
  await page.goto(path);
  await expect(page.locator("[data-shell-ready=true]")).toBeAttached({ timeout: 30_000 });
}

export async function newSession(request: APIRequestContext, body: Record<string, unknown> = {}) {
  const r = await request.post("/api/sessions", { data: body });
  expect(r.status()).toBe(201);
  return (await r.json()) as { id: string; title: string };
}

export async function settle(request: APIRequestContext, id: string, timeout = 120_000) {
  await expect
    .poll(async () => (await (await request.get(`/api/sessions/${id}`)).json()).status, { timeout })
    .not.toBe("running");
}

export async function exampleId(request: APIRequestContext): Promise<string> {
  const examples = (await (await request.get("/api/examples")).json()) as { id: string }[];
  return examples[0].id;
}

export const chatOf = (page: Page) => page.getByRole("region", { name: "Chat" });
