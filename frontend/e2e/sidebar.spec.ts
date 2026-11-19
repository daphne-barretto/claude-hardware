import { readFileSync } from "node:fs";
import path from "node:path";
import { expect, test, type Page } from "@playwright/test";

// The session page comes from the mock API's fixture session (docs/architecture.md).
const session = JSON.parse(readFileSync(path.join(__dirname, "../../fixtures/session.json"), "utf8"));
const PAGES = [
  { name: "home", path: "/" },
  { name: "New", path: "/new" },
  { name: "session", path: `/session/${session.id}` },
];

async function open(page: Page, path: string) {
  await page.goto(path);
  await expect(page.locator("[data-shell-ready=true]")).toBeAttached({ timeout: 30_000 });
}

/** Waits until the mark has loaded, so screenshots and sizes are stable. */
async function loadedMark(page: Page) {
  const mark = page.getByTestId("sidebar-wordmark").locator("img");
  await expect.poll(() => mark.evaluate((img: HTMLImageElement) => img.complete && img.naturalWidth > 0)).toBe(true);
  return mark;
}

for (const { name, path } of PAGES) {
  test(`the sidebar header shows the Claude wordmark next to the collapse button on ${name}`, async ({ page }) => {
    await open(page, path);
    const sidebar = page.getByRole("complementary", { name: "Sidebar" });
    const header = sidebar.getByTestId("sidebar-header");
    const collapse = header.getByRole("button", { name: "Hide sidebar" });
    const wordmark = header.getByTestId("sidebar-wordmark");
    await expect(collapse).toBeVisible();
    await expect(wordmark).toBeVisible();

    // The same mark as the welcome page, decorative, followed by the word "Claude" and nothing else.
    const mark = await loadedMark(page);
    await expect(mark).toHaveAttribute("src", "/claude-logo.svg");
    await expect(mark).toHaveAttribute("aria-hidden", "true");
    await expect(wordmark).toHaveText("Claude");
    // Only the collapse button and the wordmark in the header: no label or badge.
    await expect(header.locator(":scope > *")).toHaveCount(2);
    await expect(header).toHaveText("Claude");

    // Side by side on one row, wordmark right of the button.
    const b = (await collapse.boundingBox())!;
    const w = (await wordmark.boundingBox())!;
    expect(w.x).toBeGreaterThanOrEqual(b.x + b.width);
    expect(w.x - (b.x + b.width)).toBeLessThan(16);
    expect(Math.abs(w.y + w.height / 2 - (b.y + b.height / 2))).toBeLessThan(2);

    // Collapsing hides the whole header with the sidebar; reopening brings the wordmark back.
    await collapse.click();
    await expect(sidebar).toHaveCount(0);
    await expect(page.getByTestId("sidebar-wordmark")).toHaveCount(0);
    await page.getByRole("button", { name: "Show sidebar" }).first().click();
    await expect(page.getByTestId("sidebar-wordmark")).toBeVisible();
  });
}

test("the sidebar header matches the reference screenshot", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await open(page, "/new");
  await loadedMark(page);
  await expect(page.getByTestId("sidebar-header")).toHaveScreenshot("sidebar-header.png", { maxDiffPixelRatio: 0.02 });
});
