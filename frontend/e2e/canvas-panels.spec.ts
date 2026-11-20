import { expect, test, type APIRequestContext, type Locator, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";
import path from "node:path";

// Every tool panel over the 3D view is placed by the canvas layout model (chromeLayout.ts): the
// parameters panel and the dimensions stay inside the canvas, clear
// of the toolbars, with every control reachable (nothing over it); check pins stay under open
// dialogs and panels; long titles keep a title to read them by; the standards picker never
// covers the chat header's buttons. On the mock API, at a laptop with the sidebar open (the
// narrowest canvas: fix briefs 9 to 13, 16 and 17), and at a wide screen.
test.skip(!!process.env.E2E_BASE_URL, "the mock API runs only with the local dev servers");

const SESSION = JSON.parse(readFileSync(path.join(__dirname, "../../fixtures/session.json"), "utf8")) as { prompt: string };
const SIZES = [
  { width: 1280, height: 800 },
  { width: 1600, height: 1000 },
];

async function mockSession(request: APIRequestContext): Promise<string> {
  const session = await (await request.post("/api/sessions", { data: {} })).json();
  await request.post(`/api/sessions/${session.id}/messages`, { data: { text: SESSION.prompt } });
  await expect
    .poll(async () => (await (await request.get(`/api/sessions/${session.id}`)).json()).status, { timeout: 30_000 })
    .not.toBe("running");
  return session.id;
}

async function open(page: Page, id: string, size: { width: number; height: number }) {
  await page.setViewportSize(size);
  await page.goto(`/session/${id}`);
  await expect(page.locator("[data-shell-ready=true]")).toBeAttached({ timeout: 30_000 });
  const viewer = page.getByTestId("viewer");
  await expect(viewer).toHaveAttribute("data-state", "ready", { timeout: 30_000 });
  await expect(viewer).toHaveAttribute("data-animating", "false");
  const canvas = page.getByRole("region", { name: "Canvas" });
  return { viewer, canvas, tools: canvas.getByRole("toolbar", { name: "Canvas controls" }) };
}

/**
 * Problems with a panel's controls: any past the canvas's edges, or whose centre is under
 * something else (a toolbar, the view cube). A control a scrolling panel hides is scrolled to.
 */
async function unreachable(panel: Locator): Promise<string[]> {
  return panel.evaluate((el) => {
    const host = el.closest("[data-testid=hardware-canvas]")!.getBoundingClientRect();
    const out: string[] = [];
    for (const c of el.querySelectorAll<HTMLElement>("button, input, [role=radio]")) {
      if (!c.checkVisibility()) continue;
      c.scrollIntoView({ block: "nearest" });
      const r = c.getBoundingClientRect();
      const name = c.getAttribute("aria-label") ?? c.textContent?.trim();
      if (r.left < host.left - 0.5 || r.right > host.right + 0.5 || r.top < host.top - 0.5 || r.bottom > host.bottom + 0.5) {
        out.push(`${name} is past the canvas edge`);
        continue;
      }
      const top = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
      if (top && !c.contains(top) && !(top instanceof HTMLLabelElement && top.control === c)) out.push(`${name} is under ${top.closest("[aria-label]")?.getAttribute("aria-label") ?? top.tagName}`);
    }
    return out;
  });
}

/** The box of `a` and `b` overlap by more than a few pixels. */
async function overlap(a: Locator, b: Locator): Promise<boolean> {
  const [ra, rb] = [(await a.boundingBox())!, (await b.boundingBox())!];
  const w = Math.min(ra.x + ra.width, rb.x + rb.width) - Math.max(ra.x, rb.x);
  const h = Math.min(ra.y + ra.height, rb.y + rb.height) - Math.max(ra.y, rb.y);
  return w > 4 && h > 4;
}

/** Check pins (shown) whose box meets an open dialog or a panel over the 3D view. */
async function pinsUnderCovers(page: Page): Promise<string[]> {
  return page.getByTestId("hardware-canvas").evaluate((host) => {
    const covers = Array.from(host.querySelectorAll<HTMLElement>("[role=dialog], [data-pin-cover]")).filter((c) => c.checkVisibility());
    const out: string[] = [];
    for (const pin of host.querySelectorAll<HTMLElement>("[data-testid=check-pin]")) {
      if (!pin.checkVisibility({ visibilityProperty: true })) continue;
      const p = pin.getBoundingClientRect();
      for (const c of covers) {
        const r = c.getBoundingClientRect();
        const w = Math.min(p.right, r.right) - Math.max(p.left, r.left);
        const h = Math.min(p.bottom, r.bottom) - Math.max(p.top, r.top);
        if (w > 1 && h > 1) out.push(`${pin.getAttribute("aria-label")} meets ${c.getAttribute("aria-label") ?? c.tagName}`);
      }
    }
    return out;
  });
}

for (const size of SIZES) {
  test(`${size.width}×${size.height}: the parameters panel is above the toolbar and view cube, every control reachable`, async ({ page, request }) => {
    const { canvas } = await open(page, await mockSession(request), size);
    await canvas.getByRole("button", { name: "Parameters" }).click();
    const panel = canvas.getByRole("region", { name: "Parameters" });
    await expect(panel.getByRole("textbox").first()).toBeVisible();
    expect(await unreachable(panel)).toEqual([]);
  });

  test(`${size.width}×${size.height}: the dimensions stay out of the top and bottom bars, and the pins out of their way`, async ({ page, request }) => {
    const { canvas } = await open(page, await mockSession(request), size);
    await showAllPins(canvas);
    await expect(canvas.locator("[data-testid=check-pin]:visible").first()).toBeVisible();
    await canvas.getByRole("button", { name: "Dimensions", exact: true }).click();
    const dims = canvas.getByRole("group", { name: "Dimensions" });
    await expect(dims.getByRole("button").first()).toBeVisible();
    expect(await overlap(dims, canvas.getByRole("toolbar", { name: "Canvas controls" })), "dimensions over the canvas controls").toBe(false);
    // Dragging a face edits a dimension: no pin is in the way.
    await expect(canvas.getByTestId("check-pin")).toHaveCount(0);
    expect(await unreachable(dims)).toEqual([]);
  });

  test(`${size.width}×${size.height}: check pins stay under the shortcuts sheet and the panels`, async ({ page, request }) => {
    const { canvas } = await open(page, await mockSession(request), size);
    await showAllPins(canvas);
    await expect(canvas.locator("[data-testid=check-pin]:visible").first()).toBeVisible();
    expect(await pinsUnderCovers(page)).toEqual([]);
    await canvas.getByRole("button", { name: "Home view" }).focus();
    await page.keyboard.press("?");
    await expect(canvas.getByRole("dialog", { name: "Keyboard shortcuts" })).toBeVisible();
    expect(await pinsUnderCovers(page)).toEqual([]);
  });
}

/** Every check pin on the model: the Check pins switch is in the Constraints panel, closed after. */
async function showAllPins(canvas: Locator) {
  const constraints = canvas.getByRole("button", { name: "Constraints", exact: true });
  await constraints.click();
  await canvas.getByRole("radiogroup", { name: "Check pins" }).getByRole("radio", { name: "All" }).click();
  await constraints.click();
  await expect(canvas.getByTestId("constraints-panel")).toBeHidden();
}

test("long titles truncate with the full title to read, and the standards picker never covers Hide canvas", async ({ page, request }) => {
  const id = await mockSession(request);
  const title = (await (await request.patch(`/api/sessions/${id}`, { data: { title: "A very long session title that runs well past both of the headers" } })).json()).title;
  const { canvas } = await open(page, id, SIZES[0]);
  const chat = page.getByRole("region", { name: "Chat" });
  for (const heading of [chat.getByRole("heading", { level: 1 }), canvas.getByRole("heading", { level: 2 }).first()]) {
    await expect(heading).toHaveText(title);
    await expect(heading).toHaveAttribute("title", title);
    // It is cut with an ellipsis, inside its header.
    expect(await heading.evaluate((el) => el.scrollWidth > el.clientWidth)).toBe(true);
  }
  const hide = chat.getByRole("button", { name: "Hide canvas" });
  const picker = chat.getByRole("button", { name: /^Constraint set/ });
  await expect(picker).toBeVisible();
  expect(await overlap(picker, hide), "the standards picker over Hide canvas").toBe(false);
  // Hide canvas takes the click.
  const box = (await hide.boundingBox())!;
  expect(await page.evaluate(([x, y]) => document.elementFromPoint(x, y)?.closest("button")?.getAttribute("aria-label"), [box.x + box.width / 2, box.y + box.height / 2])).toBe(
    "Hide canvas",
  );
});
