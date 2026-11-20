import type { Locator, Page } from "@playwright/test";
import { answer, BOX, boardlessSession, boardDemo, openBoardDemo, exampleSession, expect, keepIfAsked, openPage, openSession, send } from "./live";

// The QA states: each is a session made on the real backend (`create`,
// through the API, as the page's browser) and the steps that open it in the page (`open`).
// The click sweep (clicks.qa.ts) opens a state again before each control, so `open` must be
// repeatable on the same session.

export type Viewport = "phone" | "laptop" | "short" | "wide";
export const VIEWPORTS: Record<Viewport, { width: number; height: number }> = {
  phone: { width: 390, height: 844 },
  laptop: { width: 1280, height: 800 },
  // A small laptop window: a short canvas, where cards anchored low on the model ran off it.
  short: { width: 1075, height: 660 },
  wide: { width: 1600, height: 1000 },
};

export type QaState = {
  id: string;
  /** What the state is, for the report. */
  what: string;
  /** Makes what the state needs on the backend; returns the session ID (or null). */
  create: (page: Page) => Promise<string | null>;
  open: (page: Page, session: string | null) => Promise<void>;
  viewports: Viewport[];
};

// The scripted Claude moves the openings demos/b-parasite/CHANGES.md lists as moved for a message
// starting with this (MOVE_OPENINGS in backend/app/e2e_api.py): a run that changes the design.
export const MOVE = "Move the openings";

const canvas = (page: Page) => page.getByRole("region", { name: "Canvas" });
const chat = (page: Page) => page.getByRole("region", { name: "Chat" });
const tool = (page: Page, name: string | RegExp) => canvas(page).getByRole("button", { name, exact: typeof name === "string" });

/** Clicks the middle of the model (a face there). */
export async function clickModel(page: Page, fx = 0.5, fy = 0.5) {
  // The nearest point to (fx, fy) where nothing (a pin, a panel) is over the 3D view.
  const at = await page.getByTestId("viewer").evaluate(
    (viewer, [fx, fy]) => {
      const r = viewer.getBoundingClientRect();
      for (let ring = 0; ring < 12; ring++)
        for (const [dx, dy] of [[0, 0], [1, 0], [-1, 0], [0, 1], [0, -1], [1, 1], [-1, -1], [1, -1], [-1, 1]]) {
          const x = r.left + r.width * fx + dx * ring * 12;
          const y = r.top + r.height * fy + dy * ring * 12;
          const top = document.elementFromPoint(x, y);
          if (top instanceof HTMLCanvasElement && viewer.contains(top)) return [x, y];
        }
      return [r.left + r.width * fx, r.top + r.height * fy];
    },
    [fx, fy],
  );
  await page.mouse.click(at[0], at[1]);
}

async function visible(loc: Locator, timeout = 15_000) {
  await expect(loc.first()).toBeVisible({ timeout });
}

type Base = { id: string; what: string; create: QaState["create"]; open: QaState["open"] };

const BASES: Record<string, Base> = {
  home: { id: "home", what: "Home", create: async () => null, open: async (page) => openPage(page, "/") },
  new: {
    id: "new",
    what: "New (empty session)",
    create: async () => null,
    open: async (page) => {
      await openPage(page, "/new");
      await visible(chat(page).getByRole("button", { name: /constraint set/i }));
    },
  },
  demo: {
    id: "demo",
    what: "A demo, opened from its link: this browser's own copy, a session like any other",
    create: async (page) => {
      const { id } = await boardDemo(page);
      // Start from the demo as it is hardcoded, not from what an earlier state did in it.
      expect((await page.request.delete(`/api/demos/${id}/session`)).status()).toBe(204);
      return id;
    },
    open: async (page, id) => {
      await openPage(page, `/demo/${id}`);
      await page.waitForURL(/\/session\/[\w-]+$/, { timeout: 30_000 });
      await visible(page.getByRole("textbox", { name: "Message" }));
    },
  },
  board: {
    id: "board",
    what: "A board session: a fresh copy of a board demo",
    create: (page) => openBoardDemo(page),
    open: (page, id) => openSession(page, id!),
  },
  question: {
    id: "question",
    what: "The board example after a message: Claude's question open",
    create: async (page) => {
      const id = await exampleSession(page);
      expect(await send(page, id, "Check the enclosure against the new revision")).toBe("waiting_for_answer");
      return id;
    },
    open: (page, id) => openSession(page, id!),
  },
  run: {
    id: "run",
    what: "The board example after a run that rebuilt it (question answered, openings moved)",
    create: async (page) => {
      const id = await exampleSession(page);
      await send(page, id, "Check the enclosure against the new revision");
      await answer(page, id, 0);
      await send(page, id, MOVE);
      return id;
    },
    open: (page, id) => openSession(page, id!),
  },
  changes: {
    // The board example's design is the contract fixture, which a run's proposal can't build on
    // with the scripted Claude, so the change set is a board-less design's: a box, then a
    // bigger box in manual mode.
    id: "changes",
    what: "A change set open for review (a manual-mode run that resized a board-less box)",
    create: async (page) => {
      const id = await boardlessSession(page);
      await keepIfAsked(page, id, await send(page, id, BOX.replace(/\d+ x/, "50 x").replace(/\d+ mm walls/, "3 mm walls"), { mode: "manual" }));
      return id;
    },
    open: async (page, id) => {
      await openSession(page, id!);
      await tool(page, "Proposed changes").click();
      await visible(page.getByRole("region", { name: "Proposed changes" }));
    },
  },
  boardless: {
    id: "boardless",
    what: `A board-less design ("${BOX}")`,
    create: (page) => boardlessSession(page),
    open: (page, id) => openSession(page, id!),
  },
};

/** A canvas tool opened on a base state. */
type Tool = { id: string; what: string; open: (page: Page) => Promise<void> };

const TOOLS_OPEN: Tool[] = [
  {
    id: "section",
    what: "Section cut",
    open: async (page) => {
      await tool(page, "Section cut").click();
      await visible(page.getByRole("region", { name: "Section" }));
    },
  },
  {
    id: "parameters",
    what: "Parameters panel",
    open: async (page) => {
      await tool(page, "Parameters").click();
      await visible(page.getByRole("region", { name: "Parameters" }));
    },
  },
  {
    id: "dimensions",
    what: "Dimensions on the model",
    open: async (page) => {
      await tool(page, "Dimensions").click();
      await visible(canvas(page).getByRole("group", { name: "Dimensions" }));
    },
  },
  {
    id: "code",
    what: "Code panel",
    open: async (page) => {
      await tool(page, "Code").click();
      await visible(page.getByRole("region", { name: "Code" }));
    },
  },
  {
    id: "checks",
    what: "Constraints panel",
    open: async (page) => {
      await tool(page, "Constraints").click();
      await visible(page.getByRole("region", { name: "Constraints" }));
    },
  },
  {
    id: "export",
    what: "Export panel",
    open: async (page) => {
      await tool(page, "Export").click();
      await visible(page.getByRole("region", { name: "Export" }));
    },
  },
  { id: "settings", what: "View settings", open: async (page) => tool(page, "View settings").click() },
  {
    id: "zoom",
    what: "Zoom menu",
    open: async (page) => {
      await tool(page, "Zoom").click();
      await visible(canvas(page).getByRole("menu", { name: "Zoom" }));
    },
  },
  {
    id: "shortcuts",
    what: "Keyboard shortcuts sheet",
    open: async (page) => {
      await tool(page, "Home view").focus();
      await page.keyboard.press("?");
      await visible(canvas(page).getByRole("dialog", { name: "Keyboard shortcuts" }));
    },
  },
  {
    id: "comment",
    what: "A comment on the model, its thread open",
    open: async (page) => {
      await canvas(page).getByRole("toolbar", { name: "Comment tools" }).getByRole("button", { name: "Comment", exact: true }).click();
      await clickModel(page, 0.45, 0.45);
      const draft = canvas(page).getByRole("dialog", { name: "New comment" });
      await visible(draft);
      await draft.getByRole("textbox", { name: "Comment text" }).fill("Can we add a light pipe here?");
      await draft.getByRole("button", { name: "Comment", exact: true }).click();
      await visible(canvas(page).getByRole("dialog", { name: "Comment thread" }));
    },
  },
  {
    id: "comments-list",
    what: "Comments list",
    open: async (page) => {
      await canvas(page).getByRole("toolbar", { name: "Comment tools" }).getByRole("button", { name: "Comments", exact: true }).click();
      await visible(page.getByRole("region", { name: "Comments list" }));
    },
  },
];

const withTool = (base: Base, t: Tool, viewports: Viewport[] = ["laptop"]): QaState => ({
  id: `${base.id}.${t.id}`,
  what: `${base.what}; ${t.what}`,
  create: base.create,
  open: async (page, id) => {
    await base.open(page, id);
    await t.open(page);
  },
  viewports,
});

const PHONE_TOOLS = new Set(["section", "export", "comment"]);
// Tools whose panels are placed by the canvas layout model (chromeLayout.ts), or that open a
// dialog the check pins must stay under: every size, from a phone to a wide screen.
const LAYOUT_TOOLS = new Set(["parameters", "dimensions", "comment", "shortcuts"]);
const toolViewports = (id: string): Viewport[] =>
  LAYOUT_TOOLS.has(id) ? ["phone", "laptop", "wide"] : PHONE_TOOLS.has(id) ? ["phone", "laptop"] : ["laptop"];

export const STATES: QaState[] = [
  { ...BASES.home, viewports: ["phone", "laptop", "wide"] },
  { ...BASES.new, viewports: ["phone", "laptop", "wide"] },
  {
    id: "new.models",
    what: "New; the model and effort picker open",
    create: async () => null,
    open: async (page) => {
      await BASES.new.open(page, null);
      await chat(page).getByRole("button", { name: /^Claude|Opus|Sonnet|Haiku|Fable/ }).first().click();
      await visible(page.getByRole("menu"));
    },
    viewports: ["phone", "laptop"],
  },
  {
    id: "search",
    what: "Search sessions",
    create: async () => null,
    open: async (page) => {
      await openPage(page, "/");
      const sidebar = page.getByRole("complementary", { name: "Sidebar" });
      if (!(await sidebar.isVisible())) await page.getByRole("button", { name: "Show sidebar" }).first().click();
      await sidebar.getByRole("button", { name: "Search" }).click();
      await visible(page.getByRole("dialog", { name: "Search sessions" }));
    },
    viewports: ["phone", "laptop"],
  },
  { ...BASES.demo, viewports: ["phone", "laptop", "wide"] },
  { ...BASES.board, viewports: ["phone", "laptop", "wide"] },
  { ...BASES.question, viewports: ["phone", "laptop"] },
  { ...BASES.run, viewports: ["laptop"] },
  { ...BASES.changes, viewports: ["phone", "laptop", "wide"] },
  {
    // A comment pinned low on the model and sent to Claude, who asks in the thread (the scripted
    // Claude asks its revision question): the card and its options stay inside the canvas.
    id: "board.comment-question",
    what: "A comment pinned low on the model, sent to Claude; Claude's question in its thread",
    // Pinned and sent once; opening the state again opens its thread from the pin.
    create: async (page) => {
      const id = await BASES.board.create(page);
      await BASES.board.open(page, id);
      await canvas(page).getByRole("toolbar", { name: "Comment tools" }).getByRole("button", { name: "Comment", exact: true }).click();
      await clickModel(page, 0.5, 0.7);
      const draft = canvas(page).getByRole("dialog", { name: "New comment" });
      await visible(draft);
      await draft.getByRole("textbox", { name: "Comment text" }).fill("Make this 1 ft tall");
      await draft.getByRole("button", { name: "Send to Claude", exact: true }).click();
      await visible(canvas(page).getByRole("dialog", { name: "Comment thread" }).getByTestId("comment-question"), 60_000);
      return id;
    },
    open: async (page, id) => {
      await BASES.board.open(page, id);
      const thread = canvas(page).getByRole("dialog", { name: "Comment thread" });
      if (!(await thread.isVisible())) await canvas(page).getByTestId("comment-pin").first().click();
      await visible(thread.getByTestId("comment-question"), 30_000);
    },
    viewports: ["laptop", "short"],
  },
  { ...BASES.boardless, viewports: ["phone", "laptop", "wide"] },
  ...TOOLS_OPEN.map((t) => withTool(BASES.board, t, toolViewports(t.id))),
  ...TOOLS_OPEN.map((t) => withTool(BASES.boardless, t, LAYOUT_TOOLS.has(t.id) ? toolViewports(t.id) : ["laptop"])),
];
