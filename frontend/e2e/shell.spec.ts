import { expect, test, type Page } from "@playwright/test";
import { DEMO_USER, EMPTY_HEADING } from "../src/components/sites/claude-shell/data";

const EMPTY_CANVAS_TEXT = "This canvas has no parts yet.";

/** Opens a page and waits until React has hydrated, so clicks reach the handlers. */
async function open(page: Page, path: string) {
  await page.goto(path);
  await expect(page.locator("[data-shell-ready=true]")).toBeAttached({ timeout: 30_000 });
}

function trackErrors(page: Page) {
  const errors: string[] = [];
  page.on("pageerror", (err) => errors.push(err.message));
  return errors;
}

test("New opens an empty hardware canvas", async ({ page }) => {
  const errors = trackErrors(page);
  await open(page, "/");
  await page.getByRole("complementary", { name: "Sidebar" }).getByRole("link", { name: "New", exact: true }).click();

  await expect(page).toHaveURL(/\/new$/);
  const chat = page.getByRole("region", { name: "Chat" });
  await expect(chat.getByText(EMPTY_HEADING)).toBeVisible();
  await expect(chat.getByRole("listitem")).toHaveCount(0);

  const canvas = page.getByTestId("hardware-canvas");
  await expect(canvas).toBeVisible();
  await expect(canvas).toHaveAttribute("data-empty", "true");
  await expect(canvas.getByText(EMPTY_CANVAS_TEXT)).toBeVisible();
  await expect(canvas.getByRole("button", { name: "Import a file" })).toBeVisible();
  await expect(page.getByRole("link", { name: "New", exact: true })).toHaveAttribute("aria-current", "page");
  expect(errors).toEqual([]);
});

test("the shell matches the mockup layout", async ({ page }) => {
  await open(page, "/new");
  const sidebar = page.getByRole("complementary", { name: "Sidebar" });
  // Only New, Search and Recents; the mockups' Projects, Artifacts, Scheduled etc. are removed.
  const nav = sidebar.getByRole("navigation", { name: "Navigation" });
  await expect(nav.getByRole("link")).toHaveText(["New"]);
  await expect(nav.getByRole("button")).toHaveText(["Search"]);
  await expect(sidebar.getByRole("button", { name: "Recents" })).toBeVisible();
  // Recents are sessions from the session API (e2e/sessions.spec.ts), not static sample pages.
  for (const href of await sidebar.getByRole("list", { name: "Recents" }).getByRole("link").evaluateAll((l) => l.map((a) => a.getAttribute("href")))) {
    expect(href).toMatch(/^\/session\//);
  }
  for (const gone of ["Projects", "Artifacts", "Scheduled", "Dispatch", "Design", "Customize", "Pinned"]) {
    await expect(sidebar.getByText(gone, { exact: true })).toHaveCount(0);
  }
  await expect(sidebar.getByText(DEMO_USER.name)).toBeVisible();
  await expect(sidebar.getByText(`· ${DEMO_USER.plan}`)).toBeVisible();

  // Chat left, canvas right, each with its own header.
  const chat = page.getByRole("region", { name: "Chat" });
  const canvas = page.getByRole("region", { name: "Canvas" });
  const chatBox = (await chat.boundingBox())!;
  const canvasBox = (await canvas.boundingBox())!;
  expect(chatBox.x + chatBox.width).toBeLessThanOrEqual(canvasBox.x + 1);
  await expect(chat.getByRole("heading", { name: "Untitled hardware" })).toBeVisible();
  await expect(canvas.getByRole("heading", { name: "Untitled" })).toBeVisible();
  for (const name of ["Share", "Expand canvas", "Close canvas"]) {
    await expect(canvas.getByRole("button", { name })).toBeVisible();
  }
  for (const name of ["Import STEP", "Photo or scan"]) {
    await expect(chat.getByRole("button", { name })).toBeVisible();
  }
  await expect(chat.getByRole("textbox", { name: "Message" })).toBeVisible();
  await expect(chat.getByRole("button", { name: "Add", exact: true })).toBeVisible();
});

test("canvas header buttons work", async ({ page, context }) => {
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  await open(page, "/new");
  const chat = page.getByRole("region", { name: "Chat" });
  const canvas = page.getByRole("region", { name: "Canvas" });

  await canvas.getByRole("button", { name: "Share" }).click();
  await expect(canvas.getByRole("button", { name: "Link copied" })).toBeVisible();
  expect(await page.evaluate(() => navigator.clipboard.readText())).toBe(page.url());

  // Expanding hides the chat but keeps its draft.
  await chat.getByRole("textbox", { name: "Message" }).fill("A draft reply");
  await canvas.getByRole("button", { name: "Expand canvas" }).click();
  await expect(chat).toBeHidden();
  await canvas.getByRole("button", { name: "Restore canvas" }).click();
  await expect(chat).toBeVisible();
  await expect(chat.getByRole("textbox", { name: "Message" })).toHaveValue("A draft reply");

  await canvas.getByRole("button", { name: "Close canvas" }).click();
  await expect(canvas).toHaveCount(0);
  await chat.getByRole("button", { name: "Show canvas" }).click();
  await expect(page.getByTestId("hardware-canvas")).toBeVisible();
});

test("file buttons attach STEP files and images, and leave anything else out with a note", async ({ page }) => {
  await open(page, "/new");
  const chat = page.getByRole("region", { name: "Chat" });

  const stepChooser = page.waitForEvent("filechooser");
  await chat.getByRole("button", { name: "Import STEP" }).click();
  const step = await stepChooser;
  expect(step.isMultiple()).toBe(true);
  expect(await step.element().getAttribute("accept")).toBe(".step,.stp");
  await step.setFiles({ name: "sample.step", mimeType: "application/octet-stream", buffer: Buffer.from("sample") });

  const importChooser = page.waitForEvent("filechooser");
  await page.getByTestId("hardware-canvas").getByRole("button", { name: "Import a file" }).click();
  await (await importChooser).setFiles([
    { name: "photo.png", mimeType: "image/png", buffer: Buffer.from("png") },
    { name: "notes.txt", mimeType: "text/plain", buffer: Buffer.from("notes") },
  ]);
  await expect(chat.getByTestId("file-note")).toHaveText(
    "notes.txt isn't supported yet. Add a STEP file (.step, .stp) or an image.",
  );

  const staged = chat.getByRole("list", { name: "Attached files" });
  await expect(staged.getByRole("listitem")).toHaveText(["sample.step", "photo.png"]);
  await staged.getByRole("button", { name: "Remove photo.png" }).click();
  await expect(staged.getByRole("listitem")).toHaveText(["sample.step"]);

  await chat.getByRole("textbox", { name: "Message" }).fill("Make a lid for this case");
  await chat.getByRole("button", { name: "Send" }).click();
  await expect(staged).toHaveCount(0);
  const sent = chat.getByRole("listitem").filter({ hasText: "Make a lid for this case" });
  await expect(sent).toContainText("sample.step");
});

test("the composer has no buttons that do nothing", async ({ page }) => {
  await open(page, "/new");
  const chat = page.getByRole("region", { name: "Chat" });
  for (const gone of ["Voice mode", "Hardware project"]) {
    await expect(page.getByRole("button", { name: gone, exact: true })).toHaveCount(0);
  }
  // The + menu offers files and photos; "Add folder" is behind a feature flag (lib/flags), off by default.
  await chat.getByRole("button", { name: "Add", exact: true }).click();
  await expect(chat.getByRole("menuitem", { name: "Add files or photos" })).toBeVisible();
  await expect(chat.getByRole("menuitem", { name: "Add folder" })).toHaveCount(0);
});
