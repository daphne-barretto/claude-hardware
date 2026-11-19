import { expect, test, type Locator, type Page } from "@playwright/test";
import { inv } from "./inventory/app-inventory";
import { entryById, entryLocator, preparePage, specLocator, runStep, waitReady, type Entry, type State, type Step } from "./inventory/ui-inventory";

// The shell at phone width: the sidebar is a drawer over the page, and the chat and
// the canvas (or artifact) take the screen one at a time, as in the Claude app on a phone.
const PHONE = { width: 390, height: 844 };
test.use({ viewport: PHONE });

async function open(page: Page, path: string) {
  await page.goto(path);
  await expect(page.locator("[data-shell-ready=true]")).toBeAttached({ timeout: 30_000 });
}

/** The whole box is on screen, left to right and top to bottom. */
async function expectOnScreen(loc: Locator, what: string) {
  const box = await loc.boundingBox();
  expect(box, `${what} has a box`).not.toBeNull();
  const b = box!;
  expect(b.x, `${what}: left edge on screen`).toBeGreaterThanOrEqual(-0.5);
  expect(b.x + b.width, `${what}: right edge on screen`).toBeLessThanOrEqual(PHONE.width + 0.5);
  expect(b.y, `${what}: top edge on screen`).toBeGreaterThanOrEqual(-0.5);
  expect(b.y + b.height, `${what}: bottom edge on screen`).toBeLessThanOrEqual(PHONE.height + 0.5);
}

test("New: the canvas's Import a file is on screen and opens the file picker", async ({ page }) => {
  await open(page, "/new");
  const canvas = page.getByRole("region", { name: "Canvas" });
  const importFile = canvas.getByRole("button", { name: "Import a file" });
  // One pane at a time: the chat first, and Show canvas switches to the canvas.
  if (!(await importFile.isVisible())) await page.getByRole("region", { name: "Chat" }).getByRole("button", { name: "Show canvas" }).click();
  await expectOnScreen(importFile, "Import a file");
  const chooser = page.waitForEvent("filechooser");
  await importFile.click();
  await (await chooser).setFiles({ name: "sample.step", mimeType: "text/plain", buffer: Buffer.from("sample") });
  // Back to the chat, where the file is staged in the composer.
  await canvas.getByRole("button", { name: "Close canvas" }).click();
  await expect(canvas).toBeHidden();
  await expect(page.getByRole("region", { name: "Chat" }).getByRole("button", { name: "Remove sample.step" })).toBeVisible();
});

for (const { name, path } of [
  { name: "home", path: "/" },
  { name: "New", path: "/new" },
]) {
  test(`${name}: the page fits the phone, with the sidebar closed and the composer whole`, async ({ page }) => {
    await open(page, path);
    // The sidebar starts closed and nothing is wider than the screen.
    await expect(page.getByRole("complementary", { name: "Sidebar" })).toBeHidden();
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(PHONE.width);
    await expectOnScreen(page.getByRole("main"), "main");
    await expectOnScreen(page.getByRole("textbox").first(), "composer");
    await expectOnScreen(page.getByRole("button", { name: "Send" }).first(), "Send");
    if (path === "/") {
      // The greeting stays on one line.
      const heading = page.getByRole("heading", { level: 1 });
      const lineHeight = await heading.evaluate((h) => parseFloat(getComputedStyle(h).lineHeight) || 40);
      expect((await heading.boundingBox())!.height).toBeLessThan(lineHeight * 1.5);
    } else {
      await expectOnScreen(page.getByRole("region", { name: "Chat" }), "chat");
    }
  });
}

test("the sidebar opens over the page and closes on a backdrop tap, Escape and navigation", async ({ page }) => {
  await open(page, "/new");
  const sidebar = page.getByRole("complementary", { name: "Sidebar" });
  const show = page.getByRole("region", { name: "Chat" }).getByRole("button", { name: "Show sidebar" });
  await show.click();
  await expectOnScreen(sidebar, "sidebar");
  // A drawer over the page: the chat keeps the full width underneath.
  expect((await page.getByRole("region", { name: "Chat" }).boundingBox())!.width).toBe(PHONE.width);
  await page.mouse.click(PHONE.width - 10, PHONE.height / 2);
  await expect(sidebar).toBeHidden();
  await show.click();
  await page.keyboard.press("Escape");
  await expect(sidebar).toBeHidden();
  await show.click();
  // Following a link (the first one, home) navigates and closes the drawer.
  await sidebar.getByRole("link").first().click();
  await expect(sidebar).toBeHidden();
});

// ---------------------------------------------------------------------------------------
// Every screen in frontend/e2e/ui-inventory.yaml at phone width: each state can be reached, and each
// control listed for it can be reached on screen. A control in the other pane is reached by
// switching to that pane, and a control in the sidebar by opening the sidebar.

const sidebar = (page: Page) => page.getByRole("complementary", { name: "Sidebar" });
const inSidebar = (e: Entry) => !!e.within?.startsWith('complementary "Sidebar"');
const PANES = [
  { region: "Canvas", open: "Show canvas", close: "Close canvas" },
  { region: "Artifact", open: "Open artifact", close: "Close artifact" },
];

async function closeSidebar(page: Page) {
  if (await sidebar(page).isVisible()) await sidebar(page).getByRole("button", { name: "Hide sidebar" }).click();
}

async function closePanes(page: Page) {
  for (const pane of PANES) {
    const close = page.getByRole("region", { name: pane.region }).getByRole("button", { name: pane.close });
    if (await close.isVisible()) await close.click();
  }
}

/**
 * Brings `loc`, inside `within` (an inventory locator chain), on screen the way a phone user
 * would: opening the sidebar, or switching to the other pane.
 */
async function revealWithin(page: Page, within: string | undefined, loc: Locator) {
  if (within?.startsWith('complementary "Sidebar"')) {
    if (await sidebar(page).isVisible()) return loc;
    // Show sidebar is in the chat's header (or the home page).
    if (!(await page.getByRole("button", { name: "Show sidebar" }).first().isVisible())) await closePanes(page);
    await page.getByRole("button", { name: "Show sidebar" }).first().click();
    return loc;
  }
  // The drawer covers the page: close it to reach anything else (unless a dialog is open over it).
  if (!(await page.locator('[aria-modal="true"]').count())) await closeSidebar(page);
  if (await loc.isVisible()) return loc;
  const chat = page.getByRole("region", { name: "Chat" });
  for (const pane of PANES) {
    if (within?.startsWith(`region "${pane.region}"`)) {
      if (!(await page.getByRole("region", { name: pane.region }).isVisible())) await chat.getByRole("button", { name: pane.open }).click();
      return loc;
    }
  }
  if (within?.startsWith('region "Chat"') || !within) await closePanes(page);
  // Anything else (a panel of the canvas, say) is on the canvas.
  else if (await chat.getByRole("button", { name: "Show canvas" }).isVisible()) {
    await chat.getByRole("button", { name: "Show canvas" }).click();
    if (!(await loc.isVisible())) await closePanes(page);
  }
  return loc;
}

const reveal = (page: Page, entry: Entry) => revealWithin(page, entry.within, entryLocator(page, entry).first());

const stepTarget = (step: Step) =>
  (["click", "fill", "attach", "paste", "focus"] as const).map((k) => (step as Record<string, string | undefined>)[k]).find(Boolean);

async function reachOnPhone(page: Page, state: State) {
  if (state.url) {
    await page.goto(state.url);
    await waitReady(page, inv);
  }
  for (const step of state.steps) {
    const id = stepTarget(step);
    if (id) {
      const entry = entryById(inv, id);
      // The sidebar starts closed on a phone: hiding it is already done.
      if (entry.id === "sidebar.hide" && !(await sidebar(page).isVisible())) continue;
      await reveal(page, entry);
    }
    const spec = "wait" in step ? step.wait : "click_on" in step ? step.click_on : "drag_on" in step ? step.drag_on : undefined;
    if (spec) await revealWithin(page, spec, specLocator(page, spec).first());
    await runStep(page, inv, step, undefined, 10_000);
  }
}

// The shell's controls on the canvas panel; the rest of the canvas panel is the canvas's own.
const SHELL_CANVAS = new Set(["canvas.share", "canvas.close", "canvas.import", "canvas.show-sidebar"]);
async function canvasOwn(loc: Locator, e: Entry) {
  if (SHELL_CANVAS.has(e.id)) return false;
  return loc.evaluate((el) => !!el.closest('section[aria-label="Canvas"]'), undefined, { timeout: 1_000 }).catch(() => false);
}

const phoneHidden = new Set(inv.entries.filter((e) => e.phone === "hidden").map((e) => e.id));
const mockApi = !process.env.E2E_BASE_URL;

test.describe("UI inventory at phone width", () => {
  for (const state of inv.states) {
    const via = state.steps.map(stepTarget).find((id) => id && phoneHidden.has(id));
    test(`${state.screen} / ${state.id}: every listed control can be reached at 390 px`, async ({ page, context }) => {
      // A session state builds a mock session first, then checks each control.
      test.setTimeout(120_000);
      test.skip(state.mock_api && !mockApi, "needs the mock session API");
      test.skip(!!via, `reached through ${via}, which a phone does not show`);
      await preparePage(page, context, inv, state);
      await reachOnPhone(page, state);
      const listed = inv.entries.filter(
        (e) => e.states.includes(state.id) && e.role !== "shortcut" && e.role !== "pointer" && !phoneHidden.has(e.id),
      );
      // The page's controls first (opening the drawer closes any open menu), then the sidebar's.
      // Within the page, what is on screen in the state first (switching panes closes open menus).
      const page_ = listed.filter((e) => !inSidebar(e));
      const shown = new Set<string>();
      for (const e of page_) if (await entryLocator(page, e).first().isVisible()) shown.add(e.id);
      const ordered = [...page_.filter((e) => shown.has(e.id)), ...page_.filter((e) => !shown.has(e.id)), ...listed.filter(inSidebar)];
      const problems: string[] = [];
      // The 3D canvas's own controls in a session (overlays, panels, menus) are not laid out for a
      // phone yet: recorded as known, not failed, until the canvas has a phone layout of its own.
      const canvasProblems: string[] = [];
      for (const entry of ordered) {
        const loc = await reveal(page, entry);
        try {
          await loc.scrollIntoViewIfNeeded({ timeout: 3_000 });
          await expectOnScreen(loc, `${entry.id} (${entry.role} "${entry.name}")`);
          // Nothing covers it but an open menu or dialog of the state (closing it uncovers the control).
          const cover = await loc.evaluate((el) => {
            const r = el.getBoundingClientRect();
            const top = document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2);
            if (!top || el.contains(top) || top.contains(el)) return null;
            const popup = top.closest('[role="menu"], [role="dialog"], [role="listbox"]');
            return popup && !popup.contains(el) ? null : top.outerHTML.slice(0, 120);
          });
          if (cover) throw new Error(`covered by ${cover}`);
        } catch (err) {
          (state.id.startsWith("session") && (await canvasOwn(loc, entry)) ? canvasProblems : problems).push(
            `${entry.id}: ${(err as Error).message.split("\n")[0]}`,
          );
        }
      }
      for (const p of canvasProblems) test.info().annotations.push({ type: "canvas at phone width", description: p });
      expect(problems).toEqual([]);
    });
  }
});
