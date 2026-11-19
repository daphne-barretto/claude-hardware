import { readFileSync } from "node:fs";
import path from "node:path";
import { PIN_FILTER_KEY } from "../src/components/canvas/pinVisibility";
import { expect, test, type APIRequestContext, type Locator, type Page } from "@playwright/test";
import { constraintItem, openConstraints, pickCheck } from "./fixtures/constraints";
import { openExample } from "./fixtures/sessions";

// Checks on the model against the mock session API, which serves the contract
// fixtures (docs/architecture.md). Scenario values come from the fixture files, never from here.
test.skip(!!process.env.E2E_BASE_URL, "the mock API runs only with the local dev servers");
// Every check's pin, not only the failed ones the canvas shows by default (the "Check pins" filter).
test.beforeEach(({ page }) => page.addInitScript((key) => localStorage.setItem(key, "all"), PIN_FILTER_KEY));

type Result = {
  id: string;
  check: string;
  standard?: string | null;
  title: string;
  status: "pass" | "fail" | "untested";
  summary: string;
  location?: { point: [number, number, number] } | null;
};
const fixture = (name: string) => JSON.parse(readFileSync(path.join(__dirname, "../../fixtures", name), "utf8"));
const CHECKS = fixture("checks.json") as { results: Result[] };
const SESSION = fixture("session.json") as { prompt: string };
const LOCATED = CHECKS.results.filter((r) => r.status !== "untested" && r.location);
const FAILED = LOCATED.filter((r) => r.status === "fail");
const UNTESTED = CHECKS.results.filter((r) => r.status === "untested");

/** The viewer's orbit centre is reported in board coordinates, rounded to 0.1 mm. */
const target = (p: [number, number, number]) =>
  p.map((v) => {
    const r = Math.round(v * 10) / 10;
    return r === 0 ? 0 : r;
  }).join(",");

async function openCanvas(page: Page, request: APIRequestContext) {
  const session = await openExample(request);
  await request.post(`/api/sessions/${session.id}/messages`, { data: { text: SESSION.prompt } });
  await expect
    .poll(async () => (await (await request.get(`/api/sessions/${session.id}/builds`)).json()).length, {
      timeout: 30_000,
    })
    .toBeGreaterThan(0);
  await page.goto(`/session/${session.id}`);
  const viewer = page.getByTestId("viewer");
  await expect(viewer).toHaveAttribute("data-state", "ready", { timeout: 30_000 });
  return { viewer, canvas: page.getByRole("region", { name: "Canvas" }) };
}

/** The checks whose pins the pointer can reach: on the model and not under a panel. */
async function reachablePins(canvas: Locator): Promise<string[]> {
  return canvas.getByTestId("check-pin").evaluateAll((pins) =>
    pins.flatMap((p) => {
      const r = p.getBoundingClientRect();
      return document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2) === p ? [p.getAttribute("data-check")!] : [];
    }),
  );
}

test("every located check result has a pin on the model", async ({ page, request }) => {
  const { canvas } = await openCanvas(page, request);
  const pins = canvas.getByTestId("check-pin");
  await expect(pins).toHaveCount(LOCATED.length);
  for (const r of LOCATED) {
    const pin = canvas.locator(`[data-testid=check-pin][data-check="${r.id}"]`);
    // On its spot: shown, or hidden where a panel is over it (fix brief 13).
    if ((await pin.getAttribute("data-covered")) !== "true") await expect(pin).toBeVisible();
    await expect(pin).toHaveAttribute("data-status", r.status);
  }
  // The Constraints panel has a row for each of them, with the worst status of its places.
  const panel = await openConstraints(canvas);
  const bar = panel.getByRole("list", { name: "Constraints" });
  await expect(bar).toBeVisible();
  for (const r of LOCATED) await expect(constraintItem(canvas, r)).toBeVisible();
  for (const r of FAILED) await expect(constraintItem(canvas, r)).toHaveAttribute("data-status", "fail");
  // Failing constraints come first.
  await expect(bar.getByTestId("constraint-item").first()).toHaveAttribute("data-status", "fail");
});

test("clicking a failure moves the camera to it and opens its card", async ({ page, request }) => {
  const { canvas, viewer } = await openCanvas(page, request);
  expect(FAILED.length).toBeGreaterThan(1);

  // From its place in the constraint's detail.
  const [first, second] = FAILED;
  await pickCheck(canvas, first);
  await expect(canvas.getByTestId("constraint-detail").locator(`button[data-check="${first.id}"]`)).toHaveAttribute("aria-pressed", "true");
  // Escape collapses the row, focus back on it; again, it closes the panel, so it covers no pin.
  await page.keyboard.press("Escape");
  await expect(canvas.getByTestId("constraint-detail")).toBeHidden();
  await expect(constraintItem(canvas, first)).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(canvas.getByTestId("constraints-panel")).toBeHidden();
  await expect(viewer).toHaveAttribute("data-target", target(first.location!.point));
  await expect(viewer).toHaveAttribute("data-zoom", "300");
  const card = canvas.getByTestId("check-card");
  await expect(card).toHaveAttribute("data-check", first.id);
  await expect(card).toContainText(first.summary);
  await expect(card.getByTestId("check-evidence")).toContainText("Measured");

  // From a pin.
  await canvas.locator(`[data-testid=check-pin][data-check="${second.id}"]`).click();
  await expect(viewer).toHaveAttribute("data-target", target(second.location!.point));
  await expect(card).toHaveAttribute("data-check", second.id);
  // The focused point is in the middle of the view, so its pin is too.
  const pin = (await canvas.locator(`[data-testid=check-pin][data-check="${second.id}"]`).boundingBox())!;
  const box = (await viewer.boundingBox())!;
  expect(Math.abs(pin.x + pin.width / 2 - (box.x + box.width / 2))).toBeLessThan(2);
  expect(Math.abs(pin.y + pin.height / 2 - (box.y + box.height / 2))).toBeLessThan(2);

  await card.getByRole("button", { name: "Close" }).click();
  await expect(card).toBeHidden();
});

test("the mouse wheel zooms when the pointer is over a pin", async ({ page, request }) => {
  const { viewer, canvas } = await openCanvas(page, request);
  await expect(canvas.getByTestId("check-pin")).toHaveCount(LOCATED.length);
  // A pin under a panel stays on its spot, under the panel: use one the pointer reaches.
  const [id] = await reachablePins(canvas);
  const pin = canvas.locator(`[data-testid=check-pin][data-check="${id}"]`);
  const box = (await pin.boundingBox())!;
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  const before = Number(await viewer.getAttribute("data-zoom"));
  await page.mouse.wheel(0, -400);
  await expect.poll(async () => Number(await viewer.getAttribute("data-zoom"))).toBeGreaterThan(before);
  // The wheel zooms, it doesn't select the pin.
  await expect(canvas.getByTestId("check-card")).toBeHidden();
});

test("a drag that starts on a pin orbits the view and doesn't select the pin", async ({ page, request }) => {
  const { viewer, canvas } = await openCanvas(page, request);
  await expect(canvas.getByTestId("check-pin")).toHaveCount(LOCATED.length);
  const reachable = await reachablePins(canvas);
  const failed = FAILED.find((r) => reachable.includes(r.id))!;
  const pin = canvas.locator(`[data-testid=check-pin][data-check="${failed.id}"]`);
  const box = (await pin.boundingBox())!;
  const [x, y] = [box.x + box.width / 2, box.y + box.height / 2];
  const view = await viewer.getAttribute("data-view");
  await page.mouse.move(x, y);
  await page.mouse.down();
  await page.mouse.move(x + 120, y + 40, { steps: 8 });
  await page.mouse.up();
  await expect(viewer).not.toHaveAttribute("data-view", view ?? "");
  await expect(viewer).toHaveAttribute("data-view", "custom");
  await expect(canvas.getByTestId("check-card")).toBeHidden();
  await expect(canvas.getByTestId("check-pin").and(canvas.locator("[aria-pressed=true]"))).toHaveCount(0);

  // A click on a pin still selects it (one the pointer reaches from the new direction).
  const [next] = await reachablePins(canvas);
  const reached = canvas.locator(`[data-testid=check-pin][data-check="${next}"]`);
  const again = (await reached.boundingBox())!;
  await page.mouse.click(again.x + again.width / 2, again.y + again.height / 2);
  await expect(reached).toHaveAttribute("aria-pressed", "true");
  await expect(canvas.getByTestId("check-card")).toBeVisible();
});

test("untested checks are listed in the Constraints panel with what they don't cover", async ({ page, request }) => {
  const { canvas } = await openCanvas(page, request);
  const panel = await openConstraints(canvas);
  await expect(panel.getByTestId("checks-summary")).toContainText(`${UNTESTED.length} untested`);
  for (const r of UNTESTED) {
    const item = constraintItem(canvas, r);
    await expect(item).toHaveAttribute("data-status", "untested");
    await item.click();
    await expect(panel.getByTestId("constraint-detail")).toContainText(r.summary);
  }
});

test("the check results match the reference screenshot", async ({ page, request }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  const { canvas } = await openCanvas(page, request);
  await pickCheck(canvas, FAILED[0], { close: true });
  await expect(canvas.getByTestId("check-card")).toBeVisible();
  await expect(canvas).toHaveScreenshot("checks-failure.png", { maxDiffPixelRatio: 0.02 });
});

// The pins never draw over the canvas panels, and every pin stays clickable.
async function pinOverlaps(page: Page) {
  return page.evaluate(() => {
    const canvas = document.querySelector('[aria-label="Canvas"]')!;
    const panels = [
      ...canvas.querySelectorAll<HTMLElement>(
        'section[aria-label="Parts"], [data-testid="view-cube"], [role="toolbar"], [role="radiogroup"][aria-label="Units"], [role="menu"]',
      ),
      canvas.querySelector<HTMLElement>('button[aria-label="Revision"]')!,
    ];
    const name = (el: HTMLElement) => el.getAttribute("aria-label") ?? el.dataset.testid ?? el.tagName;
    const out: string[] = [];
    for (const pin of canvas.querySelectorAll<HTMLElement>("[data-testid=check-pin]")) {
      // The selection ring is 2 px outside the pin.
      const p = pin.getBoundingClientRect();
      for (const el of panels) {
        const r = el.getBoundingClientRect();
        if (p.right + 2 > r.left && p.left - 2 < r.right && p.bottom + 2 > r.top && p.top - 2 < r.bottom)
          out.push(`${pin.dataset.check} over ${name(el)}`);
      }
    }
    return out;
  });
}

async function expectPinsClear(page: Page) {
  await expect.poll(() => pinOverlaps(page)).toEqual([]);
  const canvas = page.getByRole("region", { name: "Canvas" });
  // Each pin in view is the element the pointer hits at its centre, so a click selects it.
  const view = (await page.getByTestId("check-pins").boundingBox())!;
  let inView = 0;
  for (const pin of await canvas.getByTestId("check-pin").all()) {
    const box = await pin.boundingBox();
    if (!box) continue;
    const [x, y] = [box.x + box.width / 2, box.y + box.height / 2];
    if (x < view.x || x > view.x + view.width || y < view.y || y > view.y + view.height) continue;
    await pin.click({ trial: true, timeout: 2_000 });
    inView++;
  }
  expect(inView).toBeGreaterThan(0);
}

test("no pin draws over the parts panel, the view cube or the toolbar, and every pin can be clicked", async ({ page, request }) => {
  const { canvas, viewer } = await openCanvas(page, request);
  await expect(canvas.getByTestId("check-pin")).toHaveCount(LOCATED.length);
  await expect(canvas.getByRole("region", { name: "Parts" })).toBeVisible();
  await expect(canvas.getByTestId("view-cube")).toBeVisible();
  await expectPinsClear(page);

  // Zoomed in on each failure, pins near the view cube and the panels move to their edges.
  for (const r of FAILED) {
    await pickCheck(canvas, r, { close: true });
    await expect(viewer).toHaveAttribute("data-target", target(r.location!.point));
    await expect(viewer).toHaveAttribute("data-animating", "false");
    // The card is a popover next to the selected pin; close it to reach the pins under it.
    await canvas.getByTestId("check-card").getByRole("button", { name: "Close" }).click();
    await expectPinsClear(page);
    await canvas.getByRole("button", { name: "Zoom" }).click();
    for (let i = 0; i < 6; i++) await canvas.getByRole("menuitem", { name: "Zoom in" }).click();
    await expect(viewer).toHaveAttribute("data-animating", "false");
    await expect.poll(() => pinOverlaps(page)).toEqual([]);
    await canvas.getByRole("button", { name: "Zoom" }).click();
    await expectPinsClear(page);
  }

  // An open menu is a panel too.
  await canvas.getByRole("button", { name: "Revision" }).click();
  await expect(canvas.getByRole("menu", { name: "Revisions" })).toBeVisible();
  await expect.poll(() => pinOverlaps(page)).toEqual([]);
});
