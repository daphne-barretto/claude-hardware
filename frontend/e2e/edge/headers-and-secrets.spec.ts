import { expect, test } from "@playwright/test";
import { live } from "./helpers";

// What the served pages give away or allow (docs/security.md): secrets in the HTML or the
// JavaScript bundle, and whether another site can frame the app (clickjacking).
test.skip(!!process.env.E2E_BASE_URL, "the scripted backend runs only with the local dev servers");
test.beforeEach(async ({ context, baseURL }) => {
  test.setTimeout(240_000); // real builds take seconds each, more on a busy machine
  await live(context, baseURL);
});

const LEAKS = [/ANTHROPIC_API_KEY/, /sk-ant-[A-Za-z0-9_-]{10,}/, /CLAUDE_CODE_OAUTH_TOKEN/, /DEPLOY_HOOK/i, /api\.render\.com\/deploy/, /E2E_LIVE_BACKEND_URL/];

test("no secret or server env name is in the pages or their scripts", async ({ page }) => {
  const scripts = new Set<string>();
  page.on("response", (r) => {
    if (r.request().resourceType() === "script") scripts.add(r.url());
  });
  for (const path of ["/", "/new"]) {
    const html = await (await page.request.get(path)).text();
    for (const leak of LEAKS) expect(html, `${path}: ${leak}`).not.toMatch(leak);
    await page.goto(path);
    await page.waitForLoadState("networkidle");
  }
  expect(scripts.size).toBeGreaterThan(0);
  for (const url of scripts) {
    const js = await (await page.request.get(url)).text();
    for (const leak of LEAKS) expect(js, `${url}: ${leak}`).not.toMatch(leak);
  }
  const health = await (await page.request.get("/api/health")).text();
  for (const leak of LEAKS) expect(health).not.toMatch(leak);
});

test("another site can't frame the app", async ({ page }) => {
  const r = await page.request.get("/new");
  const headers = r.headers();
  const csp = headers["content-security-policy"] ?? "";
  expect(headers["x-frame-options"] ?? (/frame-ancestors/.test(csp) ? "csp" : undefined)).toBeTruthy();
});
