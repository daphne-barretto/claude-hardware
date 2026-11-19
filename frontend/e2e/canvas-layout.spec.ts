import { readFileSync } from "node:fs";
import path from "node:path";
import { expect, test, type APIRequestContext, type Page } from "@playwright/test";
import { openConstraints, pickCheck } from "./fixtures/constraints";

// The canvas chrome (top bar, parts panel, view cube, modeling toolbar, tool panels, bottom bar,
// view label, the Constraints panel) at laptop sizes with the sidebar open, on the mock's build and on
// demos (with and without a board), with the section tool on: the canvas never scrolls and
// nothing overlaps. Also the section without a board, check pins on the model and the pin
// filter. Everything expected comes from fixtures/, never from here.
test.skip(!!process.env.E2E_BASE_URL, "the mock API runs only with the local dev servers");

const root = path.join(__dirname, "../..");
const read = (file: string) => JSON.parse(readFileSync(path.join(root, file), "utf8"));
const SESSION = read("fixtures/session.json") as { prompt: string };
type Result = { id: string; check: string; standard?: string | null; status: string; title: string; component: string | null; location?: unknown };
type Sample = { id: string; build: { board: unknown | null; id: string }; results: Result[] };
const SAMPLES: Sample[] = (read("demos/sessions/index.json").samples as string[]).map((id) => {
  const latest = read(`demos/sessions/${id}/session.json`).session.latest_build_id as string;
  return {
    id,
    build: read(`demos/sessions/${id}/builds/${latest}/build.json`),
    results: read(`demos/sessions/${id}/builds/${latest}/checks.json`).results,
  };
});
// A demo on a board with failed and passed checks, and one without a board with untested checks.
const WITH_BOARD = SAMPLES.find((s) => s.build.board && s.results.some((r) => r.status === "fail") && s.results.some((r) => r.status === "pass"))!;
const NO_BOARD = SAMPLES.find((s) => !s.build.board && s.results.some((r) => r.status === "untested"))!;
const label = (r: Result) => (r.component ? `${r.title} · ${r.component}` : r.title);

const SIZES = [
  { width: 1280, height: 800 },
  { width: 1440, height: 900 },
  { width: 1500, height: 750 },
];

async function mockSession(request: APIRequestContext): Promise<string> {
  const session = await (await request.post("/api/sessions", { data: {} })).json();
  await request.post(`/api/sessions/${session.id}/messages`, { data: { text: SESSION.prompt } });
  await expect
    .poll(async () => (await (await request.get(`/api/sessions/${session.id}`)).json()).status, { timeout: 30_000 })
    .not.toBe("running");
  return session.id;
}

async function open(page: Page, id: string, size = SIZES[0]) {
  await page.setViewportSize(size);
  // A demo opens as this browser's own copy (`/demo/<id>`), a session like any other.
  await page.goto(SAMPLES.some((s) => s.id === id) ? `/demo/${id}` : `/session/${id}`);
  await expect(page.locator("[data-shell-ready=true]")).toBeAttached({ timeout: 30_000 });
  const viewer = page.getByTestId("viewer");
  await expect(viewer).toHaveAttribute("data-state", "ready", { timeout: 30_000 });
  await expect(viewer).toHaveAttribute("data-animating", "false");
  const canvas = page.getByRole("region", { name: "Canvas" });
  return { viewer, canvas, tools: canvas.getByRole("toolbar", { name: "Inspection tools" }) };
}

// The chrome's pieces, and where each one is visible (cut to the boxes that clip it).
const CHROME: Record<string, string> = {
  revision: 'button[aria-label="Revision"]',
  compare: 'button[aria-label^="Compare with"]',
  changes: 'button[aria-label="Proposed changes"]',
  units: '[role=radiogroup][aria-label="Units"]',
  controls: '[role=toolbar][aria-label="Canvas controls"]',
  parts: 'section[aria-label="Parts"]',
  cube: "[data-testid=view-cube]",
  modeling: '[role=toolbar][aria-label="Modeling tools"]',
  section: 'section[aria-label="Section"]',
  measure: 'section[aria-label="Measure"]',
  layout: '[role=radiogroup][aria-label="Layout"]',
  inspection: '[role=toolbar][aria-label="Inspection tools"]',
  comments: '[role=toolbar][aria-label="Comment tools"]',
  triad: "[data-testid=axis-triad]",
  viewLabel: "[data-testid=view-label]",
  modelParts: "[data-testid=model-parts]",
  modelSize: "[data-testid=model-size]",
};

type Box = { name: string; x0: number; y0: number; x1: number; y1: number };

async function chrome(page: Page) {
  return page.getByTestId("hardware-canvas").evaluate((host, sel) => {
    const out: Box[] = [];
    const outside: string[] = [];
    const h = host.getBoundingClientRect();
    for (const [name, s] of Object.entries(sel)) {
      for (const el of host.querySelectorAll<HTMLElement>(s)) {
        if (!el.checkVisibility()) continue;
        // Text: where the text is, not the block around it.
        let r: DOMRect;
        if (["viewLabel", "modelParts", "modelSize"].includes(name)) {
          const range = document.createRange();
          range.selectNodeContents(el);
          r = range.getBoundingClientRect();
        } else r = el.getBoundingClientRect();
        let [x0, y0, x1, y1] = [r.left, r.top, r.right, r.bottom];
        for (let a: HTMLElement | null = el; a && a !== host.parentElement; a = a.parentElement) {
          const st = getComputedStyle(a);
          if (st.overflowX === "visible" && st.overflowY === "visible") continue;
          const c = a.getBoundingClientRect();
          [x0, y0, x1, y1] = [Math.max(x0, c.left), Math.max(y0, c.top), Math.min(x1, c.right), Math.min(y1, c.bottom)];
        }
        if (x1 - x0 < 1 || y1 - y0 < 1) continue;
        if (r.left < h.left - 0.5 || r.right > h.right + 0.5 || r.top < h.top - 0.5 || r.bottom > h.bottom + 0.5)
          outside.push(`${name} [${Math.round(r.left - h.left)}, ${Math.round(r.right - h.left)}] in ${Math.round(h.width)}`);
        out.push({ name, x0, y0, x1, y1 });
      }
    }
    const overlaps: string[] = [];
    for (let i = 0; i < out.length; i++)
      for (let j = i + 1; j < out.length; j++) {
        const [a, b] = [out[i], out[j]];
        const w = Math.min(a.x1, b.x1) - Math.max(a.x0, b.x0);
        const hgt = Math.min(a.y1, b.y1) - Math.max(a.y0, b.y0);
        if (w > 1 && hgt > 1) overlaps.push(`${a.name} × ${b.name} (${Math.round(w)}×${Math.round(hgt)})`);
      }
    return { names: out.map((b) => b.name), overlaps, outside };
  }, CHROME);
}

/** The canvas never scrolls: no overflow, and focusing any control leaves it where it is. */
async function expectNoScroll(page: Page) {
  const state = await page.getByTestId("hardware-canvas").evaluate((host) => {
    for (const el of host.querySelectorAll<HTMLElement>("button, [tabindex]")) el.focus();
    host.querySelector<HTMLElement>("[data-testid=zoom-level]")?.scrollIntoView();
    return { sl: host.scrollLeft, st: host.scrollTop, sw: host.scrollWidth, cw: host.clientWidth };
  });
  expect(state.sl, "the canvas scrolled sideways").toBe(0);
  expect(state.st, "the canvas scrolled down").toBe(0);
  expect(state.sw, "something in the canvas is wider than it").toBeLessThanOrEqual(state.cw);
}

async function expectClean(page: Page, must: string[]) {
  const c = await chrome(page);
  for (const name of must) expect(c.names, `${name} is shown`).toContain(name);
  expect(c.outside, "chrome past the canvas edges").toEqual([]);
  expect(c.overlaps, "chrome that overlaps").toEqual([]);
  await expectNoScroll(page);
}

for (const size of SIZES) {
  test(`${size.width}×${size.height}: the mock build's chrome never overlaps or scrolls with section and measure on`, async ({ page, request }) => {
    const { tools } = await open(page, await mockSession(request), size);
    await tools.getByRole("button", { name: "Section cut" }).click();
    await expect(page.getByTestId("section-clearance")).toBeVisible();
    await tools.getByRole("button", { name: "Measure" }).click();
    await expectClean(page, ["parts", "cube", "modeling", "section", "measure", "layout", "inspection", "comments", "viewLabel", "modelParts", "controls"]);
    // The zoom value is in full view.
    const zoom = (await page.getByTestId("zoom-level").boundingBox())!;
    const host = (await page.getByTestId("hardware-canvas").boundingBox())!;
    expect(zoom.x + zoom.width).toBeLessThanOrEqual(host.x + host.width);
  });

  test(`${size.width}×${size.height}: a board demo with section on and its Constraints panel open, and a board-less demo with its untested checks`, async ({ page }) => {
    let { tools } = await open(page, WITH_BOARD.id, size);
    await tools.getByRole("button", { name: "Section cut" }).click();
    await expect(page.getByTestId("section-clearance")).toBeVisible();
    await expectClean(page, ["parts", "modeling", "section", "layout", "inspection"]);
    // The Constraints panel, a row expanded, opens inside the canvas, and nothing covers it.
    const canvas = page.getByRole("region", { name: "Canvas" });
    await openConstraints(canvas);
    await canvas.getByTestId("constraint-item").first().click();
    await expectOnTop(page, canvas.getByTestId("constraints-panel"));
    await expectClean(page, ["parts", "modeling", "section", "layout", "inspection"]);

    ({ tools } = await open(page, NO_BOARD.id, size));
    await tools.getByRole("button", { name: "Section cut" }).click();
    await expect(page.getByTestId("section-plane")).toBeVisible();
    // Its Constraints panel lists the untested checks.
    const panel = await openConstraints(page.getByRole("region", { name: "Canvas" }));
    await expect(panel.locator('[data-testid=constraint-item][data-status="untested"]').first()).toBeVisible();
    await expectClean(page, ["parts", "modeling", "section", "layout", "inspection"]);
    await expectOnTop(page, panel);
  });
}

/** A panel: inside the canvas, and nothing covers it. */
async function expectOnTop(page: Page, popover: ReturnType<Page["getByTestId"]>) {
  await expect(popover).toBeVisible();
  const box = (await popover.boundingBox())!;
  const host = (await page.getByTestId("hardware-canvas").boundingBox())!;
  expect(box.x).toBeGreaterThanOrEqual(host.x);
  expect(box.y).toBeGreaterThanOrEqual(host.y);
  expect(box.x + box.width).toBeLessThanOrEqual(host.x + host.width + 0.5);
  expect(box.y + box.height).toBeLessThanOrEqual(host.y + host.height + 0.5);
  const covered = await popover.evaluate((el) => {
    const r = el.getBoundingClientRect();
    const spots = [0.1, 0.5, 0.9].flatMap((fx) => [0.1, 0.5, 0.9].map((fy) => [r.left + fx * r.width, r.top + fy * r.height]));
    return spots.filter(([x, y]) => !el.contains(document.elementFromPoint(x, y))).length;
  });
  expect(covered, "spots of the popover under something else").toBe(0);
}

test("turning the section on and off keeps the user's view", async ({ page, request }) => {
  const { viewer, tools } = await open(page, await mockSession(request));
  const view = async () => ({
    zoom: await viewer.getAttribute("data-zoom"),
    target: await viewer.getAttribute("data-target"),
    rect: await viewer.getAttribute("data-model-rect"),
  });
  const before = await view();
  await tools.getByRole("button", { name: "Section cut" }).click();
  await expect(viewer).not.toHaveAttribute("data-section", "");
  expect(await view()).toEqual(before);
  await tools.getByRole("button", { name: "Section cut" }).click();
  await expect(viewer).toHaveAttribute("data-section", "");
  expect(await view()).toEqual(before);
});

test("a design without a board is cut through its own parts, and never asks for a board", async ({ page }) => {
  const boards: string[] = [];
  page.on("request", (r) => r.url().endsWith("/board") && boards.push(r.url()));
  const { viewer, canvas, tools } = await open(page, NO_BOARD.id);
  await tools.getByRole("button", { name: "Section cut" }).click();
  const panel = canvas.getByRole("region", { name: "Section" });
  await expect(panel.getByTestId("section-plane")).toBeVisible();
  await expect(viewer).not.toHaveAttribute("data-section", "");
  await expect(panel).not.toContainText(/board/i);
  await expect(panel).not.toContainText(/loading/i);
  // The picker lists the design's parts.
  const mesh = read(`demos/sessions/${NO_BOARD.id}/builds/${NO_BOARD.build.id}/mesh.gltf`) as { nodes: { extras?: { part?: string } }[] };
  const parts = mesh.nodes.flatMap((n) => (n.extras?.part ? [n.extras.part] : []));
  await panel.getByRole("button", { name: "Section through" }).click();
  await expect(panel.getByRole("menuitemradio")).toHaveText(parts);
  expect(boards).toEqual([]);
});

test("a board that can't be loaded says so plainly instead of loading forever", async ({ page, request }) => {
  const id = await mockSession(request);
  await page.route("**/board", (route) => route.fulfill({ status: 500, body: "{}" }));
  const { canvas, tools } = await open(page, id);
  await tools.getByRole("button", { name: "Section cut" }).click();
  const panel = canvas.getByRole("region", { name: "Section" });
  await expect(panel).toContainText("Couldn't load the board, so it isn't shown in the section.");
  await expect(panel).not.toContainText(/loading/i);
  // The section still works, through the design's parts.
  await expect(panel.getByTestId("section-plane")).toBeVisible();
});

test("check pins: failed only by default, all or none, the selected one always, remembered", async ({ page }) => {
  const { canvas } = await open(page, WITH_BOARD.id);
  const pins = canvas.getByTestId("check-pin");
  // The filter is in the Constraints panel's header.
  await openConstraints(canvas);
  const filter = canvas.getByRole("radiogroup", { name: "Check pins" });
  await expect(filter.getByRole("radio", { name: "Failed" })).toHaveAttribute("aria-checked", "true");
  // On their spots (some under the open panel, so hidden there).
  await expect(pins.first()).toBeAttached();
  expect(new Set(await pins.evaluateAll((els) => els.map((e) => e.getAttribute("data-status"))))).toEqual(new Set(["fail"]));

  await filter.getByRole("radio", { name: "All" }).click();
  await expect(pins.and(page.locator('[data-status="pass"]')).first()).toBeAttached();
  await filter.getByRole("radio", { name: "None" }).click();
  await expect(pins).toHaveCount(0);

  // Picking a passed check in its constraint's detail shows its pin although the filter hides passes.
  const pass = WITH_BOARD.results.find((r) => r.status === "pass" && r.location)!;
  await pickCheck(canvas, pass, { close: true });
  await expect(canvas.getByTestId("check-card")).toContainText(label(pass));
  await expect(pins).toHaveCount(1);
  await expect(pins.first()).toHaveAttribute("data-check", pass.id);
  await canvas.getByTestId("check-card").getByRole("button", { name: "Show on model" }).click();
  await expect(pins.first()).toHaveAttribute("data-check", pass.id);

  // The choice is kept for this browser.
  await page.reload();
  await expect(page.getByTestId("viewer")).toHaveAttribute("data-state", "ready", { timeout: 30_000 });
  await openConstraints(canvas);
  await expect(canvas.getByRole("radiogroup", { name: "Check pins" }).getByRole("radio", { name: "None" })).toHaveAttribute("aria-checked", "true");
  await expect(pins).toHaveCount(0);
});

test("every check pin sits on the model, in every layout", async ({ page }) => {
  const { viewer, canvas } = await open(page, WITH_BOARD.id, SIZES[1]);
  await openConstraints(canvas);
  await canvas.getByRole("radiogroup", { name: "Check pins" }).getByRole("radio", { name: "All" }).click();
  // Closed, so the panel covers no pin.
  await page.keyboard.press("Escape");
  await expect(canvas.getByTestId("constraints-panel")).toBeHidden();
  const layouts = canvas.getByRole("radiogroup", { name: "Layout" }).getByRole("radio");
  for (let i = 0; i < (await layouts.count()); i++) {
    await layouts.nth(i).click();
    await expect(viewer).toHaveAttribute("data-animating", "false");
    await page.waitForTimeout(300);
    const rect = ((await viewer.getAttribute("data-model-rect")) ?? "").split(",").map(Number);
    const vb = (await viewer.boundingBox())!;
    const pins = await canvas.getByTestId("check-pin").evaluateAll((els) =>
      els.map((e) => {
        const r = e.getBoundingClientRect();
        return { id: e.getAttribute("data-check"), x: r.left + r.width / 2, y: r.top + r.height / 2 };
      }),
    );
    expect(pins.length).toBeGreaterThan(0);
    // On the model's outline on screen, give or take a pin's width.
    const slack = 24;
    const off = pins.filter(
      (p) =>
        p.x < vb.x + rect[0] - slack ||
        p.x > vb.x + rect[0] + rect[2] + slack ||
        p.y < vb.y + rect[1] - slack ||
        p.y > vb.y + rect[1] + rect[3] + slack,
    );
    expect(off.map((p) => p.id), `pins off the model in layout ${i}`).toEqual([]);
  }
});

test("an opened demo has the modeling tools, as any session of one's own", async ({ page }) => {
  const { canvas } = await open(page, WITH_BOARD.id);
  await expect(canvas.getByRole("toolbar", { name: "Modeling tools" })).toBeVisible();
  await expect(canvas.getByTestId("modeling-read-only")).toHaveCount(0);
});
